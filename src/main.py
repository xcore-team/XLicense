from __future__ import annotations

import logging

from fastapi import APIRouter
from xcore.kernel.events import Event
from xcore.sdk import (
    AutoDispatchMixin,
    TrustedBase,
    action,
    error,
    ok,
    validate_payload,
)

from .models.license import Base, LicenseState
from .repositories.license import LicenseRepository
from .routes import build_router
from .services.events import XLicenseEvents
from .services.jwt import LicenseTokenService
from .services.license import LicenseService
from .services.seed import seed_license_plans
from .state_machine import LicenseStateMachineError

logger = logging.getLogger("xlicense.plugin")


# ── IPC payload schemas ───────────────────────────────────────────────────────

VALIDATE_LICENSE_SCHEMA: dict = {
    "license_key": (str, ...),
}

TRANSITION_SCHEMA: dict = {
    "license_id": (str, ...),
    "to_state": (str, ...),
    "reason": (str, None),
}

GET_TENANT_LICENSES_SCHEMA: dict = {
    "tenant_id": (str, ...),
}


class Plugin(AutoDispatchMixin, TrustedBase):
    """
    XLicense Plugin — gestion du cycle de vie des licences par tenant.

    Features:
        - JWT RS256 (clé partagée avec xauth ou clé dédiée)
        - State machine : TRIAL → ACTIVE → SUSPENDED / EXPIRED / REVOKED
        - Middleware ASGI injectant X-License-* headers + request.state.license_*
        - Cache xcore (Redis) pour éviter les requêtes DB à chaque requête
        - IPC xcore : xlicense.validate, xlicense.transition, xlicense.get_tenant_licenses
        - Routes REST : CRUD licences + plans + endpoint public /verify
    """

    async def _initialize(self, db) -> None:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await seed_license_plans(db.session())

    async def on_load(self) -> None:
        env = self.ctx.env
        db = self.get_service("db")
        cache = self.get_service("cache")

        self._db = db
        self._cache = cache

        # initialiser la base de données et les données de départ
        await self._initialize(db)

        # Token service
        self._token_service = LicenseTokenService(
            private_key_path=env["JWT_PRIVATE_KEY_PATH"],
            public_key_path=env["JWT_PUBLIC_KEY_PATH"],
        )

        # Events
        self._events = XLicenseEvents(self.ctx.events)

        # Router
        self.app = build_router(
            db,
            cache,
            self._token_service,
            caller=self.ctx.caller,
            events=self._events,
        )

        # Sweep planifié : expire les licences échues toutes les heures
        scheduler = self.get_service("scheduler")
        if scheduler:

            @scheduler.interval(hours=1)
            async def license_sweep():
                async with self._db.session() as session:
                    svc = LicenseService(
                        self._token_service,
                        LicenseRepository(session),
                        cache_service=self._cache,
                        events=self._events,
                    )
                    expired = await svc.expire_stale()
                    await session.commit()
                    if expired:
                        logger.info(
                            "Sweep auto : %d licence(s) expirée(s)",
                            len(expired),
                        )

        @self.ctx.events.on("xlicense.config")
        async def _on_config(event: Event) -> dict:
            return self.ctx.config

        @self.ctx.events.on("xlicense.verify")
        async def _on_verify(event: Event) -> dict | None:
            tenant_id = event.data.get("tenant_id")
            if not tenant_id:
                return None
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                return await svc.get_active_license_for_tenant(tenant_id)

        @self.ctx.events.on("xauth.tenant.created")
        async def _on_tenant_created(event: Event) -> None:
            tenant_id = event.data.get("tenant_id")
            plan_id = event.data.get("plan_id")
            if not tenant_id:
                return
            async with self._db.session() as session:
                from .repositories.license import LicensePlanRepository
                from .schemas import LisenseCreate
                from .services.seed import seed_license_plans

                plan_repo = LicensePlanRepository(session)
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    plan_repository=plan_repo,
                    cache_service=self._cache,
                    events=self._events,
                )

                existing = await svc.get_for_tenant(tenant_id)
                if existing:
                    return

                plan = await plan_repo.get(plan_id) if plan_id else None
                if plan is None:
                    plans = await plan_repo.all()
                    if not plans:
                        await seed_license_plans(session)
                        plans = await plan_repo.all()
                    plan = plans[0] if plans else None
                if plan is None:
                    return

                await svc.create_license(
                    LisenseCreate(
                        tenant_id=tenant_id,
                        plan_id=str(plan.id),
                        state="trial",
                        expires_at=30,
                        extra={"source": "tenant_onboarding"},
                    )
                )
                await session.commit()

    async def on_unload(self) -> None:
        pass

    def get_router(self) -> APIRouter | None:
        return self.app

    # ── IPC ───────────────────────────────────────────────────────────────────

    @action("xlicense.validate")
    @validate_payload(VALIDATE_LICENSE_SCHEMA, type_response="model", unset=False)
    async def _ipc_validate(self, payload) -> dict:
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                result = await svc.validate(payload.license_key)
            return ok(**result)
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.transition")
    @validate_payload(TRANSITION_SCHEMA, type_response="model", unset=False)
    async def _ipc_transition(self, payload) -> dict:
        try:
            to_state = LicenseState(payload.to_state)
        except ValueError:
            return error(f"État inconnu : {payload.to_state}", code="invalid_state")
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                result = await svc.transition(
                    payload.license_id,
                    to_state,
                    reason=getattr(payload, "reason", None),
                )
                await session.commit()
            return ok(license=result.model_dump())
        except LicenseStateMachineError as exc:
            return error(str(exc), code="invalid_transition")
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.get_tenant_licenses")
    @validate_payload(GET_TENANT_LICENSES_SCHEMA, type_response="model", unset=False)
    async def _ipc_get_tenant_licenses(self, payload) -> dict:
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                licenses = await svc.get_for_tenant(payload.tenant_id)
            return ok(licenses=[l.model_dump() for l in licenses])
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.get_plans")
    async def _ipc_get_plans(self, payload) -> dict:
        try:
            async with self._db.session() as session:
                from .repositories.license import LicensePlanRepository

                plans = await LicensePlanRepository(session).all()
            return ok(
                plans=[
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "type": p.type.value,
                        "price": p.price,
                        "max_users": p.max_users,
                        "max_machines": p.max_machines,
                        "features": p.features or {},
                        "quotas": p.quotas or {},
                        "description": p.description,
                    }
                    for p in plans
                ]
            )
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.expire_stale")
    async def _ipc_expire_stale(self, payload) -> dict:
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                expired = await svc.expire_stale()
                await session.commit()
            return ok(expired_count=len(expired), license_ids=expired)
        except Exception as exc:
            return error(str(exc), code="error")

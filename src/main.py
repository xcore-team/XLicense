from __future__ import annotations

from fastapi import APIRouter
from xcore.sdk import AutoDispatchMixin, TrustedBase, action, ok, error, validate_payload
from .services.seed import seed_license_plans

from .routes import build_router

from .models.license import Base, LicenseState
from .repositories.license import LicenseRepository
from .services.jwt import LicenseTokenService
from .services.license import LicenseService
from .state_machine import LicenseStateMachineError
from xcore.kernel.events import Event
from .middleware import LicenseMiddleware


# ── IPC payload schemas ───────────────────────────────────────────────────────

VALIDATE_LICENSE_SCHEMA: dict = {
    "license_key": (str, ...),
}

TRANSITION_SCHEMA: dict = {
    "license_id": (str, ...),
    "to_state":   (str, ...),
    "reason":     (str, None),
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

        # Router
        self.app = build_router(db, cache, self._token_service)

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
                )
                return await svc.get_active_license_for_tenant(tenant_id)

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
                )
                licenses = await svc.get_for_tenant(payload.tenant_id)
            return ok(licenses=[l.model_dump() for l in licenses])
        except Exception as exc:
            return error(str(exc), code="error")
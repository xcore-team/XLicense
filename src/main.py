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
    schema,
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


def _available_modes(plan) -> list[str]:
    """Modes de paiement proposés par le plan, selon les ids Stripe renseignés."""
    modes: list[str] = []
    if plan.stripe_price_id:
        modes.append("subscription")
    if plan.stripe_product_id:
        modes.append("one_time")
    return modes


def _billing_mode(plan) -> str:
    """Mode par défaut (rétro-compat) : le premier mode disponible."""
    modes = _available_modes(plan)
    return modes[0] if modes else "unconfigured"


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

    async def _initialize(self, db, stripe_map: dict | None = None) -> None:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await seed_license_plans(db.session(), stripe_map=stripe_map)

    @staticmethod
    def _stripe_map_from_env(env) -> dict:
        """Construit le mapping plan→Stripe depuis l'environnement (ids optionnels)."""
        def g(key: str) -> str | None:
            val = env.get(key) if env else None
            return val or None

        # Chaque plan peut porter les deux ids : price_id (abonnement récurrent)
        # et product_id (paiement unique). L'utilisateur choisit ensuite le mode
        # au checkout parmi available_modes.
        return {
            "Basic Plan": {
                "price_id": g("STRIPE_PRICE_BASIC"),
                "product_id": g("STRIPE_PRODUCT_BASIC"),
            },
            "Pro Plan": {
                "price_id": g("STRIPE_PRICE_PRO"),
                "product_id": g("STRIPE_PRODUCT_PRO"),
            },
            "Enterprise Plan": {
                "price_id": g("STRIPE_PRICE_ENTERPRISE"),
                "product_id": g("STRIPE_PRODUCT_ENTERPRISE"),
            },
            "Lifetime Plan": {
                "price_id": g("STRIPE_PRICE_LIFETIME"),
                "product_id": g("STRIPE_PRODUCT_LIFETIME"),
            },
        }

    async def on_load(self) -> None:
        env = self.ctx.env
        db = self.get_service("db")
        cache = self.get_service("cache")

        self._db = db
        self._cache = cache

        # initialiser la base de données et les données de départ
        await self._initialize(db, stripe_map=self._stripe_map_from_env(env))

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
        # (scheduler optionnel — peut être désactivé dans la config)
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
    @schema(version="1.0", input={"license_key": (str, ...)}, output={"valid": bool, "state": str, "reason": str, "license_id": str, "tenant_id": str, "expires_at": str}, type_response="model", unset=False)
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
    @schema(
        version="1.0",
        input={"license_id": (str, ...), "to_state": (str, ...), "reason": (str, None)},
        output={"license": dict},
        type_response="model",
        unset=False,
    )
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

    @action("xlicense.renew")
    @schema(
        version="1.0",
        input={"license_id": (str, ...), "extend_days": (int, 365), "reason": (str, None)},
        output={"license": dict},
        type_response="model",
        unset=False,
    )
    async def _ipc_renew(self, payload) -> dict:
        """
        Renouvellement d'une licence — déclenché par xpayproxy après paiement
        (webhook invoice.paid / subscription). Étend l'échéance et réactive.
        """
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                result = await svc.renew(
                    payload.license_id,
                    extend_days=getattr(payload, "extend_days", 365),
                    reason=getattr(payload, "reason", None) or "Renouvellement après paiement",
                )
                await session.commit()
            return ok(license=result.model_dump())
        except LicenseStateMachineError as exc:
            return error(str(exc), code="invalid_transition")
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.entitlements.get")
    @schema(
        version="1.0",
        input={"tenant_id": (str, ...)},
        output={
            "tenant_id": str,
            "valid": bool,
            "state": str,
            "modules": list,
            "features": dict,
            "quotas": dict,
        },
        type_response="model",
        unset=False,
    )
    async def _ipc_get_entitlements(self, payload) -> dict:
        """Entitlement effectif d'un tenant = modules/features/quotas de son plan actif.

        Source de vérité de « quels modules l'organisation a le droit d'utiliser ».
        modules == ["*"] signifie tous les modules. Un tenant sans licence active
        renvoie valid=False et modules=[].
        """
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                data = await svc.get_active_license_for_tenant(payload.tenant_id)
            if not data:
                return ok(
                    tenant_id=payload.tenant_id,
                    valid=False,
                    state="unknown",
                    modules=[],
                    features={},
                    quotas={},
                )
            state = data.get("state", "unknown")
            return ok(
                tenant_id=payload.tenant_id,
                valid=state in ("active", "trial"),
                state=state,
                modules=data.get("modules", []),
                features=data.get("features", {}),
                quotas=data.get("quotas", {}),
            )
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.get_tenant_licenses")
    @schema(version="1.0", input={"tenant_id": (str, ...)}, output={"licenses": list}, type_response="model", unset=False)
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
    @schema(version="1.0", output={"plans": list})
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
                        "modules": p.modules or [],
                        "description": p.description,
                        "stripe_price_id": p.stripe_price_id,
                        "stripe_product_id": p.stripe_product_id,
                        "billing_mode": _billing_mode(p),
                        "available_modes": _available_modes(p),
                    }
                    for p in plans
                ]
            )
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.change_plan")
    @schema(
        version="1.0",
        input={"license_id": (str, ...), "plan_id": (str, ...), "reason": (str, None), "activate": (bool, True)},
        output={"license": dict},
        type_response="model",
        unset=False,
    )
    async def _ipc_change_plan(self, payload) -> dict:
        """Bascule une licence sur un autre plan — déclenché par xpayproxy."""
        try:
            async with self._db.session() as session:
                svc = LicenseService(
                    self._token_service,
                    LicenseRepository(session),
                    cache_service=self._cache,
                    events=self._events,
                )
                result = await svc.change_plan(
                    payload.license_id,
                    payload.plan_id,
                    reason=getattr(payload, "reason", None),
                    activate=getattr(payload, "activate", True),
                )
                await session.commit()
            return ok(license=result.model_dump())
        except LicenseStateMachineError as exc:
            return error(str(exc), code="invalid_transition")
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.resolve_plan")
    @schema(
        version="1.0",
        input={"price_id": (str, None), "product_id": (str, None)},
        output={"plan_id": str, "name": str, "type": str, "billing_mode": str},
        type_response="model",
        unset=False,
    )
    async def _ipc_resolve_plan(self, payload) -> dict:
        """Résout un plan depuis son tarif Stripe (price_id ou product_id)."""
        try:
            from .repositories.license import LicensePlanRepository

            async with self._db.session() as session:
                plan = await LicensePlanRepository(session).get_by_stripe(
                    price_id=getattr(payload, "price_id", None),
                    product_id=getattr(payload, "product_id", None),
                )
            if plan is None:
                return error("Aucun plan ne correspond à ce tarif Stripe", code="not_found")
            return ok(
                plan_id=str(plan.id),
                name=plan.name,
                type=plan.type.value,
                billing_mode=_billing_mode(plan),
            )
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.set_plan_mapping")
    @schema(version="1.0", input={"mappings": (list, [])}, output={"updated": int})
    async def _ipc_set_plan_mapping(self, payload) -> dict:
        """Synchronise le mapping plan→Stripe depuis xpayproxy (seed au démarrage).

        payload = {"mappings": [{"name": str, "price_id": str|None,
        "product_id": str|None}, ...]}. Écrase les ids pour rester aligné sur le
        catalogue Stripe (source de vérité). Plans introuvables ignorés.
        """
        try:
            from .repositories.license import LicensePlanRepository

            mappings = (payload or {}).get("mappings", []) if isinstance(payload, dict) else []
            updated = 0
            async with self._db.session() as session:
                repo = LicensePlanRepository(session)
                for m in mappings:
                    name = m.get("name")
                    if not name:
                        continue
                    plan = await repo.get_by_name(name)
                    if plan is None:
                        continue
                    plan.stripe_price_id = m.get("price_id")
                    plan.stripe_product_id = m.get("product_id")
                    await repo.save(plan)
                    updated += 1
                await session.commit()
            return ok(updated=updated)
        except Exception as exc:
            return error(str(exc), code="error")

    @action("xlicense.expire_stale")
    @schema(version="1.0", output={"expired_count": int, "license_ids": list})
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

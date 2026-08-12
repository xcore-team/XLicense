from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from xcore.kernel.api import AuthPayload, get_current_user
from xcore.sdk import require_permission

from .repositories.license import LicensePlanRepository, LicenseRepository
from .schemas import LicenseResponse, PlanCreateRequest
from .services.events import XLicenseEvents
from .services.jwt import LicenseTokenService
from .services.license import LicenseService
from .models.license import LicenseState
from .state_machine import LicenseStateMachine, LicenseStateMachineError


class VerifyLicenseRequest(BaseModel):
    license_key: str


def _plan_available_modes(plan) -> list[str]:
    """Modes de paiement proposés par le plan, selon les ids Stripe renseignés.

    Un plan peut proposer les deux : abonnement (stripe_price_id récurrent) et/ou
    paiement unique (stripe_product_id). Le frontend laisse l'utilisateur choisir.
    """
    modes: list[str] = []
    if plan.stripe_price_id:
        modes.append("subscription")
    if plan.stripe_product_id:
        modes.append("one_time")
    return modes


def _plan_billing_mode(plan) -> str:
    """Mode par défaut (rétro-compat) : le premier mode disponible."""
    modes = _plan_available_modes(plan)
    return modes[0] if modes else "unconfigured"


def _plan_to_dict(p) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "type": p.type.value,
        "price": p.price,
        "max_users": p.max_users,
        "max_machines": p.max_machines,
        "features": p.features or {},
        "quotas": p.quotas or {},
        "description": p.description,
        "stripe_price_id": p.stripe_price_id,
        "stripe_product_id": p.stripe_product_id,
        "billing_mode": _plan_billing_mode(p),
        "available_modes": _plan_available_modes(p),
    }


def build_router(
    db,
    cache,
    token_service: LicenseTokenService,
    caller: Callable[[str, str, dict], Awaitable[dict]] | None = None,
    events: XLicenseEvents | None = None,
) -> APIRouter:
    router = APIRouter(tags=["xlicense"])

    def _svc(session) -> LicenseService:
        return LicenseService(
            token_service,
            LicenseRepository(session),
            plan_repository=LicensePlanRepository(session),
            cache_service=cache,
            events=events,
        )

    async def _tenant_permissions(
        user: AuthPayload, tenant_id: str
    ) -> tuple[bool, bool]:
        perms = user.get("permissions", [])
        if "admin:*" in perms:
            return True, True
        if caller is None:
            return False, False
        result = await caller(
            "auth",
            "xauth.tenant_access",
            {
                "user_id": user["sub"],
                "tenant_id": tenant_id,
                "permissions": perms,
            },
        )
        return (
            result.get("has_access", False),
            result.get("can_manage", False),
        )

    async def _license_permissions(
        session, user: AuthPayload, license_id: str
    ) -> tuple[object, bool, bool]:
        repo = LicenseRepository(session)
        lic = await repo.get_by_license_id(license_id)
        if lic is None:
            raise HTTPException(status_code=404, detail="Licence non trouvée")
        has_access, can_manage = await _tenant_permissions(user, str(lic.tenant_id))
        return lic, has_access, can_manage

    # ── Public ────────────────────────────────────────────────────────────────

    @router.post("/verify", summary="Vérifier une clé de licence (public)")
    async def verify_license(body: VerifyLicenseRequest) -> dict:
        async with db.session() as session:
            result = await _svc(session).validate(body.license_key)
            await session.commit()
        return result

    @router.get("/me", summary="Licence du tenant courant")
    async def my_license(
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        # /me est exclu du middleware de licence (pour ne jamais s'auto-bloquer en
        # 402) : on résout donc la licence ici, sans dépendre de request.state.
        tenant_id = current_user.get("tenant_id") or current_user.get("user", {}).get(
            "tenant_id"
        )
        if not tenant_id:
            return {
                "valid": False,
                "state": "unknown",
                "tenant_id": None,
                "expires_at": None,
            }

        async with db.session() as session:
            license_data = await _svc(session).get_active_license_for_tenant(tenant_id)
            await session.commit()

        if not license_data:
            return {
                "valid": False,
                "state": "unknown",
                "tenant_id": tenant_id,
                "expires_at": None,
            }

        state = license_data.get("state", "unknown")
        try:
            # is_usable (ACTIVE ou TRIAL) — pas `state == "active"`, qui
            # bloquait l'app pour tout tenant encore en période d'essai
            # (même bug que le middleware avant correctif, cf. audit
            # XLicense — même source de vérité `LicenseState.is_usable`).
            valid = LicenseState(state).is_usable
        except ValueError:
            valid = False
        return {
            "valid": valid,
            "state": state,
            "tenant_id": tenant_id,
            "license_id": license_data.get("id"),
            "expires_at": license_data.get("expires_at"),
            "features": license_data.get("features", {}),
            "quotas": license_data.get("quotas", {}),
            "modules": license_data.get("modules", []),
            # Détail des licences cumulées (Pro + add-on, etc.) — le champ
            # ci-dessus reste un résumé fusionné pour la compatibilité
            # descendante des consommateurs existants.
            "active_licenses": license_data.get("active_licenses", []),
        }

    # ── Plans ─────────────────────────────────────────────────────────────────

    @router.post(
        "/plans",
        status_code=status.HTTP_201_CREATED,
        summary="Créer un plan",
    )
    async def create_plan(
        body: PlanCreateRequest,
        _: AuthPayload = Depends(require_permission("license:manage")),
    ) -> dict:
        from .models.license import LicensePlan, LicenseType

        async with db.session() as session:
            try:
                plan_type = LicenseType(body.type)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Type de licence invalide : {body.type}")

            plan = LicensePlan(
                name=body.name,
                type=plan_type,
                price=body.price,
                max_users=body.max_users,
                max_machines=body.max_machines,
                description=body.description,
                features=body.features,
                quotas=body.quotas,
                modules=body.modules,
            )
            repo = LicensePlanRepository(session)
            saved = await repo.save(plan)
            await session.commit()
            return {
                "id": str(saved.id),
                "name": saved.name,
                "type": saved.type.value,
                "price": saved.price,
                "max_users": saved.max_users,
                "max_machines": saved.max_machines,
                "features": saved.features or {},
                "quotas": saved.quotas or {},
                "modules": saved.modules or [],
            }

    @router.get("/plans", summary="Lister les plans")
    async def list_plans() -> list:
        async with db.session() as session:
            repo = LicensePlanRepository(session)
            plans = await repo.all()
            return [_plan_to_dict(p) for p in plans]

    @router.patch(
        "/plans/{plan_id}",
        summary="Mettre à jour le mapping Stripe d'un plan (admin)",
    )
    async def update_plan(
        plan_id: str,
        body: dict,
        _: AuthPayload = Depends(require_permission("license:manage")),
    ) -> dict:
        from uuid import UUID

        async with db.session() as session:
            repo = LicensePlanRepository(session)
            plan = await repo.get(UUID(str(plan_id)))
            if plan is None:
                raise HTTPException(status_code=404, detail="Plan introuvable")
            if "stripe_price_id" in body:
                plan.stripe_price_id = body["stripe_price_id"]
            if "stripe_product_id" in body:
                plan.stripe_product_id = body["stripe_product_id"]
            for field in ("price", "max_users", "max_machines", "description"):
                if field in body:
                    setattr(plan, field, body[field])
            await session.commit()
            return _plan_to_dict(plan)

    # ── Licences ──────────────────────────────────────────────────────────────
    #
    # Pas de route HTTP de création / activation / renouvellement : ces opérations
    # donnent de la valeur à une licence et ne doivent JAMAIS être déclenchables
    # par un utilisateur (tout owner de tenant a le rôle admin → admin:*, donc
    # un gate par permission ne suffit pas à les distinguer d'un admin plateforme).
    #
    # Cycle de vie d'une licence — uniquement via chemins de confiance :
    #   • trial 30j      → handler serveur xauth.tenant.created (onboarding)
    #   • activation     → xpayproxy webhook paiement → IPC xlicense.transition
    #   • renouvellement → xpayproxy webhook invoice.paid → IPC xlicense.renew
    # Ces actions IPC sont plugin-à-plugin et ne sont pas exposées en HTTP.

    @router.get(
        "/licenses/tenant/{tenant_id}",
        summary="Licences d'un tenant",
    )
    async def get_tenant_licenses(
        tenant_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> list:
        async with db.session() as session:
            has_access, _ = await _tenant_permissions(current_user, tenant_id)
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            licenses = await _svc(session).get_for_tenant(tenant_id)
        return [lic.model_dump() for lic in licenses]

    @router.get("/licenses/{license_id}", response_model=LicenseResponse)
    async def get_license(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            _, has_access, _ = await _license_permissions(
                session, current_user, license_id
            )
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            result = await _svc(session).get_by_id(license_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Licence non trouvée")
        return result

    # ── State transitions ─────────────────────────────────────────────────────
    # `activate` (→ ACTIVE) est volontairement absent de l'API HTTP : redonner de
    # la valeur à une licence ne peut venir que du paiement (IPC xlicense.transition).
    # Seules les transitions « vers l'arrêt » (suspend / revoke) restent gérables.

    @router.post("/licenses/{license_id}/suspend", summary="Suspendre")
    async def suspend(
        license_id: str,
        body: dict = {},
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(
                    session, current_user, license_id
                )
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).suspend(
                    license_id, reason=body.get("reason")
                )
                await session.commit()
                return result
            except (ValueError, LicenseStateMachineError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/licenses/{license_id}/revoke", summary="Révoquer")
    async def revoke(
        license_id: str,
        body: dict = {},
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(
                    session, current_user, license_id
                )
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).revoke(
                    license_id, reason=body.get("reason")
                )
                await session.commit()
                return result
            except (ValueError, LicenseStateMachineError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    # `renew` (extension d'échéance) n'est pas exposé en HTTP : un tenant ne peut
    # se renouveler que par un paiement validé (xpayproxy → IPC xlicense.renew).

    @router.post(
        "/licenses/{license_id}/rotate-key",
        summary="Rotation de clé JWT",
    )
    async def rotate_key(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(
                    session, current_user, license_id
                )
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                lic = await _svc(session).get_by_id(license_id)
                # Mesure la dernière ROTATION, pas la dernière vérification
                # (`last_validation_at` est mis à jour par /verify, appelé en
                # continu par des agents — le cooldown était quasiment
                # toujours actif, sans rapport avec la fréquence réelle de
                # rotation. Audit XLicense Constat 5).
                if lic and lic.last_rotated_at:
                    from datetime import datetime, timezone
                    elapsed = (datetime.now(timezone.utc) - lic.last_rotated_at).total_seconds()
                    if elapsed < 86400:
                        raise HTTPException(status_code=429, detail="Rotation déjà effectuée dans les 24h")
                result = await _svc(session).rotate_key(license_id)

                await session.commit()
                return result
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc))

    # ── Admin transition ──────────────────────────────────────────────────────

    @router.post(
        "/admin/licenses/{license_id}/transition",
        summary="Forcer une transition d'état (admin)",
    )
    async def admin_transition(
        license_id: str,
        body: dict,
        _: AuthPayload = Depends(require_permission("license:manage")),
    ) -> LicenseResponse:
        to_state = body.get("to_state")
        reason = body.get("reason", "transition admin")
        if not to_state:
            raise HTTPException(status_code=422, detail="to_state requis")
        async with db.session() as session:
            try:
                result = await _svc(session).transition(
                    license_id, LicenseState(to_state), reason=reason
                )
                await session.commit()
                return result
            except (ValueError, LicenseStateMachineError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    # ── Admin sweep ───────────────────────────────────────────────────────────

    @router.post(
        "/admin/expire-stale",
        summary="Expirer les licences échues (sweep)",
    )
    async def expire_stale(
        _: AuthPayload = Depends(require_permission("license:manage")),
    ) -> dict:
        async with db.session() as session:
            expired = await _svc(session).expire_stale()
            await session.commit()
        return {
            "expired_count": len(expired),
            "license_ids": expired,
        }

    # ── Introspection ─────────────────────────────────────────────────────────

    @router.get(
        "/licenses/{license_id}/transitions",
        summary="Transitions disponibles",
    )
    async def allowed_transitions(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        async with db.session() as session:
            lic, has_access, _ = await _license_permissions(
                session, current_user, license_id
            )
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            sm = LicenseStateMachine(lic)
            return {
                "current_state": lic.state.value,
                "allowed_transitions": [s.value for s in sm.allowed_transitions()],
            }

    return router

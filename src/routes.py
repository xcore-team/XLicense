from fastapi import APIRouter, Depends, HTTPException, Request, status
from xcore.kernel.api import AuthPayload, get_current_user
from xcore.sdk import require_permission


from .repositories.license import LicenseRepository, LicensePlanRepository
from .schemas import LicenseResponse, LisenseCreate
from .services.jwt import LicenseTokenService
from .services.license import LicenseService
from .state_machine import LicenseStateMachine, LicenseStateMachineError
from app.xauth.src.services.tenant import TenantService



def build_router(db, cache, token_service: LicenseTokenService) -> APIRouter:
    router = APIRouter(tags=["xlicense"])

    def _svc(session) -> LicenseService:
        return LicenseService(
            token_service,
            LicenseRepository(session),
            plan_repository=LicensePlanRepository(session),
            cache_service=cache,
        )

    async def _tenant_permissions(session, user: AuthPayload, tenant_id: str) -> tuple[bool, bool]:
        perms = user.get("permissions", [])
        if "admin:*" in perms:
            return True, True
        tenant_service = TenantService(session, cache=cache)
        has_access = await tenant_service.user_has_access(user["sub"], tenant_id)
        can_manage = await tenant_service.user_can_manage(user["sub"], tenant_id, perms)
        return has_access, can_manage

    async def _license_permissions(session, user: AuthPayload, license_id: str) -> tuple[object, bool, bool]:
        repo = LicenseRepository(session)
        lic = await repo.get_by_license_id(license_id)
        if lic is None:
            raise HTTPException(status_code=404, detail="Licence non trouvée")
        has_access, can_manage = await _tenant_permissions(session, user, str(lic.tenant_id))
        return lic, has_access, can_manage

    # ── Public ────────────────────────────────────────────────────────────────

    @router.post("/verify", summary="Vérifier une clé de licence (public)")
    async def verify_license(request: Request) -> dict:
        """
        Endpoint public — vérifie la validité d'une licence.
        Appelé par les agents/machines pour valider leur clé.
        """
        body = await request.json()
        license_key = body.get("license_key", "")
        if not license_key:
            raise HTTPException(status_code=422, detail="license_key requis")

        async with db.session() as session:
            result = await _svc(session).validate(license_key)
            await session.commit()
        return result

    @router.get("/me", summary="Licence du tenant courant")
    async def my_license(request: Request) -> dict:
        """
        Retourne l'état de la licence injecté par le middleware.
        Pratique pour les frontends.
        """
        return {
            "valid": getattr(request.state, "license_valid", False),
            "state": getattr(request.state, "license_state", "unknown"),
            "tenant_id": getattr(request.state, "license_tenant_id", None),
        }

    # ── Plans ─────────────────────────────────────────────────────────────────

    @router.post("/plans", status_code=status.HTTP_201_CREATED, summary="Créer un plan")
    async def create_plan(
        body: dict,
        _: AuthPayload = Depends(require_permission("admin:*")),
    ) -> dict:
        from .models.license import LicensePlan, LicenseType
        async with db.session() as session:
            plan = LicensePlan(
                name=body["name"],
                type=LicenseType(body["type"]),
                max_users=body.get("max_users", 1),
                max_machines=body.get("max_machines", 1),
            )
            repo = LicensePlanRepository(session)
            saved = await repo.save(plan)
            await session.commit()
            return {
                "id": str(saved.id),
                "name": saved.name,
                "type": saved.type.value,
                "max_users": saved.max_users,
                "max_machines": saved.max_machines,
            }

    @router.get("/plans", summary="Lister les plans")
    async def list_plans(
        _: AuthPayload = Depends(get_current_user),
    ) -> list:
        async with db.session() as session:
            repo = LicensePlanRepository(session)
            plans = await repo.all()
            return [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "type": p.type.value,
                    "max_users": p.max_users,
                    "max_machines": p.max_machines,
                }
                for p in plans
            ]

    # ── Licences ──────────────────────────────────────────────────────────────

    @router.post("/licenses", response_model=LicenseResponse, status_code=status.HTTP_201_CREATED)
    async def create_license(
        body: LisenseCreate,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            _, can_manage = await _tenant_permissions(session, current_user, body.tenant_id)
            if not can_manage:
                raise HTTPException(status_code=403, detail="Access denied")
            result = await _svc(session).create_license(body)
            await session.commit()
        return result

    @router.get("/licenses/tenant/{tenant_id}", summary="Licences d'un tenant")
    async def get_tenant_licenses(
        tenant_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> list:
        async with db.session() as session:
            has_access, _ = await _tenant_permissions(session, current_user, tenant_id)
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            licenses = await _svc(session).get_for_tenant(tenant_id)
        return [l.model_dump() for l in licenses]

    @router.get("/licenses/{license_id}", response_model=LicenseResponse)
    async def get_license(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            _, has_access, _ = await _license_permissions(session, current_user, license_id)
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            result = await _svc(session).get_by_id(license_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Licence non trouvée")
        return result

    # ── State transitions ─────────────────────────────────────────────────────

    @router.post("/licenses/{license_id}/activate", summary="Activer")
    async def activate(
        license_id: str,
        body: dict = {},
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(session, current_user, license_id)
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).activate(license_id, reason=body.get("reason"))
                await session.commit()
                return result
            except (ValueError, LicenseStateMachineError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/licenses/{license_id}/suspend", summary="Suspendre")
    async def suspend(
        license_id: str,
        body: dict = {},
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(session, current_user, license_id)
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).suspend(license_id, reason=body.get("reason"))
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
                _, _, can_manage = await _license_permissions(session, current_user, license_id)
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).revoke(license_id, reason=body.get("reason"))
                await session.commit()
                return result
            except (ValueError, LicenseStateMachineError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/licenses/{license_id}/renew", summary="Renouveler")
    async def renew(
        license_id: str,
        body: dict = {},
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(session, current_user, license_id)
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).renew(
                    license_id,
                    extend_days=body.get("extend_days", 365),
                    reason=body.get("reason"),
                )
                await session.commit()
                return result
            except (ValueError, LicenseStateMachineError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/licenses/{license_id}/rotate-key", summary="Rotation de clé JWT")
    async def rotate_key(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            try:
                _, _, can_manage = await _license_permissions(session, current_user, license_id)
                if not can_manage:
                    raise HTTPException(status_code=403, detail="Access denied")
                result = await _svc(session).rotate_key(license_id)
                await session.commit()
                return result
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc))

    # ── Admin sweep ───────────────────────────────────────────────────────────

    @router.post("/admin/expire-stale", summary="Expirer les licences échues (sweep)")
    async def expire_stale(
        _: AuthPayload = Depends(require_permission("admin:*")),
    ) -> dict:
        """
        Parcourt toutes les licences ACTIVE/TRIAL dont expires_at < now
        et les passe à EXPIRED. À appeler via un scheduler ou manuellement.
        """
        async with db.session() as session:
            expired = await _svc(session).expire_stale()
            await session.commit()
        return {"expired_count": len(expired), "license_ids": expired}

    # ── Allowed transitions (introspection) ───────────────────────────────────

    @router.get("/licenses/{license_id}/transitions", summary="Transitions disponibles")
    async def allowed_transitions(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        async with db.session() as session:
            lic, has_access, _ = await _license_permissions(session, current_user, license_id)
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            sm = LicenseStateMachine(lic)
            return {
                "current_state": lic.state.value,
                "allowed_transitions": [s.value for s in sm.allowed_transitions()],
            }

    return router

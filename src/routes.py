import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from xcore.kernel.api import AuthPayload, get_current_user
from xcore.sdk import require_permission

logger = logging.getLogger("xlicense.routes")

from .repositories.license import LicensePlanRepository, LicenseRepository
from .schemas import LicenseResponse, PlanCreateRequest
from .services.events import XLicenseEvents
from .services.jwt import LicenseTokenService
from .services.license import LicenseService
from .models.license import LicenseState
from .state_machine import LicenseStateMachine, LicenseStateMachineError


class VerifyLicenseRequest(BaseModel):
    license_key: str


class ConfirmPaymentRequest(BaseModel):
    """L'identifiant de session rendu par le prestataire au retour du paiement.

    C'est la seule chose que le navigateur envoie, et elle n'est pas crue : la session est
    revérifiée chez le prestataire, et ses métadonnées doivent désigner l'organisation et le
    module de l'appelant.
    """

    session_id: str = Field(..., min_length=1, max_length=255)


class ModuleUpsertRequest(BaseModel):
    """Mettre un module en rayon, ou changer son prix (liste fermée de champs).

    `code` est le nom du plugin (1er segment de route) : c'est la clé partagée avec
    `license_plans.modules`, le gate par module du middleware et le `source_plugin` des
    permissions d'xauth. On ne l'invente pas, on le reprend tel quel.
    """

    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    label: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)
    free: Optional[bool] = None
    price: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    period: Optional[str] = Field(None, pattern=r"^(month|year|once)$")
    stripe_price_id: Optional[str] = Field(None, max_length=255)
    active: Optional[bool] = None


def _module_to_dict(m) -> dict:
    return {
        "code": m.code,
        "label": m.label,
        "description": m.description,
        "free": bool(m.free),
        "price": float(m.price or 0),
        "currency": m.currency,
        "period": m.period.value if m.period else None,
        "stripe_price_id": m.stripe_price_id,
        "active": bool(m.active),
        # Un module payant sans tarif Stripe n'est pas vendable : l'écran doit le dire à l'admin.
        "sellable": bool(m.payable or m.free),
    }


class PlanUpdateRequest(BaseModel):
    """Champs modifiables d'un plan (liste fermée, valeurs bornées).

    Tout autre champ du corps est ignoré (id, nom, type, fonctionnalités…).
    """

    stripe_price_id: Optional[str] = Field(None, max_length=255)
    stripe_product_id: Optional[str] = Field(None, max_length=255)
    price: Optional[float] = Field(None, ge=0)
    max_users: Optional[int] = Field(None, ge=1)
    max_machines: Optional[int] = Field(None, ge=1)
    description: Optional[str] = Field(None, max_length=2000)


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
    get_service: Callable[[str], Any] | None = None,
    config: dict | None = None,
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

    def _require_platform_admin(user: AuthPayload) -> None:
        """Suspendre / révoquer : réservé à l'administration de la plateforme.

        Le propriétaire d'un tenant a `can_manage` sur SA licence (rotation de
        clé), mais ne doit pas pouvoir couper ou détruire sa propre licence.
        `tenant_admin` ne porte ni admin:* ni license:manage.
        """
        perms = user.get("permissions", []) or []
        if "admin:*" not in perms and "license:manage" not in perms:
            raise HTTPException(
                status_code=403,
                detail="Réservé aux administrateurs de la plateforme",
            )

    def _mask_key(lic: LicenseResponse) -> LicenseResponse:
        """Membre sans droit de gestion : l'état de la licence reste visible,
        pas la clé elle-même (ni son empreinte) : seuls les 4 derniers
        caractères servent à la reconnaître."""
        return lic.model_copy(
            update={"license_key": "****" + lic.license_key[-4:], "license_hash": ""}
        )

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

    # ── Les modules de MON organisation (la marketplace) ──────────────────────
    #
    # Lire : tout membre de l'organisation. Prendre ou rendre un module : son ADMINISTRATEUR
    # (`can_manage` d'`xauth.tenant_access` : propriétaire, rôle `tenant_admin`, ou
    # `license:write|manage`). Comme `/me`, ces routes sont hors du gate pour ne jamais
    # s'auto-bloquer : une organisation doit pouvoir prendre un module même bloquée en 402.

    def _current_tenant(user: AuthPayload) -> str:
        tenant_id = user.get("tenant_id") or user.get("user", {}).get("tenant_id")
        if not tenant_id:
            raise HTTPException(status_code=400, detail="Aucune organisation courante")
        return tenant_id

    async def _require_manage(user: AuthPayload, tenant_id: str) -> None:
        has_access, can_manage = await _tenant_permissions(user, tenant_id)
        if not has_access:
            raise HTTPException(status_code=403, detail="Access denied")
        if not can_manage:
            raise HTTPException(
                status_code=403,
                detail="Réservé aux administrateurs de l'organisation",
            )

    @router.get("/me/modules", summary="Les modules de mon organisation (marketplace)")
    async def my_modules(current_user: AuthPayload = Depends(get_current_user)) -> dict:
        from .repositories.license import (LicenseModuleRepository,
                                           LicenseTenantModuleRepository)

        tenant_id = _current_tenant(current_user)
        async with db.session() as session:
            has_access, can_manage = await _tenant_permissions(current_user, tenant_id)
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            license_data = await _svc(session).get_active_license_for_tenant(tenant_id)
            catalog = await LicenseModuleRepository(session).in_catalog()
            taken = {
                row.module_code: row
                for row in await LicenseTenantModuleRepository(session).for_tenant(tenant_id)
            }
            await session.commit()

        state = (license_data or {}).get("state", "unknown")
        try:
            licence_ok = LicenseState(state).is_usable
        except ValueError:
            licence_ok = False
        # Sans licence utilisable, rien n'est utilisable — quoi que dise la liste des modules. Deuxième
        # ligne de défense : la source est corrigée (`get_active_license_for_tenant` vide l'entitlement),
        # mais un écran ne doit pas dépendre d'un seul rempart pour ne pas annoncer un module comme actif.
        entitled = set((license_data or {}).get("modules", [])) if licence_ok else set()
        # L'essai ouvre tout : le dire, sinon l'écran afficherait « non pris » sur des modules
        # que l'organisation utilise réellement en ce moment.
        everything = "*" in entitled
        return {
            "tenant_id": tenant_id,
            "state": state,
            "valid": licence_ok,
            "trial": state == LicenseState.TRIAL.value,
            "all_modules": everything,
            "can_manage": can_manage,
            "modules": [
                {
                    **_module_to_dict(m),
                    "taken": m.code in taken,
                    "source": taken[m.code].source.value if m.code in taken else None,
                    "expires_at": (
                        taken[m.code].expires_at.isoformat()
                        if m.code in taken and taken[m.code].expires_at
                        else None
                    ),
                    "usable": everything or m.code in entitled,
                }
                for m in catalog
            ],
        }

    @router.post("/me/modules/{code}", summary="Prendre un module (gratuit : immédiat)")
    async def take_module(
        code: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from .models.license import LicenseTenantModule, ModuleSource
        from .repositories.license import (LicenseModuleRepository,
                                           LicenseTenantModuleRepository)

        tenant_id = _current_tenant(current_user)
        async with db.session() as session:
            await _require_manage(current_user, tenant_id)
            module = await LicenseModuleRepository(session).get_by_code(code)
            if module is None or not module.active:
                raise HTTPException(status_code=404, detail="Module introuvable au catalogue")

            repo = LicenseTenantModuleRepository(session)
            existing = await repo.get_one(tenant_id, code)
            if existing is not None and existing.is_current(datetime.now(tz=timezone.utc)):
                return {"code": code, "taken": True, "source": existing.source.value}

            if not module.free:
                # Un module payant ne s'active pas ici : il s'achète. On rend ce qu'il faut à
                # l'écran pour ouvrir le paiement, sans rien activer (402 = « paiement requis »).
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "payment_required",
                        "module": code,
                        "price": float(module.price or 0),
                        "currency": module.currency,
                        "period": module.period.value if module.period else None,
                        "stripe_price_id": module.stripe_price_id,
                    },
                )

            if existing is not None:  # échéance passée : on relance l'abonnement gratuit
                existing.source = ModuleSource.FREE
                existing.expires_at = None
            else:
                await repo.save(
                    LicenseTenantModule(
                        tenant_id=tenant_id, module_code=code, source=ModuleSource.FREE
                    )
                )
            await _svc(session).invalidate_tenant(tenant_id)
            await session.commit()
        return {"code": code, "taken": True, "source": ModuleSource.FREE.value}

    @router.delete("/me/modules/{code}", summary="Rendre un module")
    async def drop_module(
        code: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from .models.license import ModuleSource
        from .repositories.license import LicenseTenantModuleRepository

        tenant_id = _current_tenant(current_user)
        async with db.session() as session:
            await _require_manage(current_user, tenant_id)
            repo = LicenseTenantModuleRepository(session)
            row = await repo.get_one(tenant_id, code)
            if row is None:
                raise HTTPException(status_code=404, detail="Module non pris par cette organisation")
            if row.source == ModuleSource.PAID:
                # Un module payé se résilie chez le prestataire, pas ici : le retirer d'un trait
                # couperait l'accès d'une période déjà réglée.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "subscription_active",
                        "module": code,
                        "stripe_item_id": row.stripe_item_id,
                    },
                )
            await repo.delete(row)
            await _svc(session).invalidate_tenant(tenant_id)
            await session.commit()
        return {"code": code, "taken": False}

    # ── Payer un module (xpay) ────────────────────────────────────────────────
    #
    # Un abonnement PAR MODULE : c'est ce que le service `ext.xpay` sait faire (`subcribe` ouvre
    # une session d'abonnement sur un tarif, `portal` laisse l'abonné gérer et résilier chez le
    # prestataire). Le prix est forfaitaire par module (décision du 3 nov. 2026).
    #
    # Rien n'est activé sur la foi du client : la confirmation REVÉRIFIE la session chez le
    # prestataire, et n'accepte que si ses métadonnées désignent CETTE organisation et CE module —
    # sans quoi un identifiant de session d'autrui déverrouillerait un module.

    def _pay():
        pay = get_service("ext.xpay") if get_service else None
        if pay is None:
            raise HTTPException(
                status_code=503,
                detail="Paiement indisponible : l'extension `ext.xpay` n'est pas montée",
            )
        return pay

    def _payer_email(user: AuthPayload) -> str:
        email = (user.get("user") or {}).get("email") or user.get("email")
        if not email:
            raise HTTPException(
                status_code=400, detail="Aucune adresse sur ce compte : paiement impossible"
            )
        return email

    @router.post("/me/modules/{code}/checkout", summary="Ouvrir le paiement d'un module")
    async def checkout_module(
        code: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from .repositories.license import LicenseModuleRepository

        tenant_id = _current_tenant(current_user)
        async with db.session() as session:
            await _require_manage(current_user, tenant_id)
            module = await LicenseModuleRepository(session).get_by_code(code)
            if module is None or not module.active:
                raise HTTPException(status_code=404, detail="Module introuvable au catalogue")
            if module.free:
                raise HTTPException(
                    status_code=400, detail="Ce module est gratuit : il s'active sans paiement"
                )
            if not module.stripe_price_id:
                raise HTTPException(
                    status_code=409, detail="Module payant sans tarif chez le prestataire"
                )
            price_id, currency, price = module.stripe_price_id, module.currency, float(module.price or 0)

        # Où le prestataire renvoie l'abonné. Construit ICI, à partir d'un réglage de l'hôte : accepter une
        # adresse de retour du navigateur ouvrirait une redirection arbitraire. Le fragment est conservé et
        # `?session_id=` s'y ajoute, donc le routeur à dièse du Desk le lit comme une requête de route.
        desk = ((config or {}).get("desk_url") or "").rstrip("/")
        back = f"{desk}/#/billing?module={code}" if desk else "/success"
        cancelled = f"{desk}/#/billing?module={code}&cancelled=1" if desk else "/cancel"
        try:
            result = await _pay().subcribe(
                price_id,
                _payer_email(current_user),
                success_path=back,
                cancel_path=cancelled,
                # Les métadonnées sont la seule chose que la confirmation croira : elles viennent
                # d'ici, jamais du navigateur.
                metadata={"tenant_id": tenant_id, "module_code": code},
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - panne du prestataire : jamais une 500 muette
            logger.warning("xpay checkout refusé (%s/%s) : %s", tenant_id, code, exc)
            raise HTTPException(status_code=502, detail="Le prestataire de paiement a refusé la demande")
        return {
            "url": result.url,
            "session_id": result.session_id,
            "module": code,
            "price": price,
            "currency": currency,
        }

    @router.post("/me/modules/{code}/confirm", summary="Confirmer un module payé")
    async def confirm_module(
        code: str,
        body: ConfirmPaymentRequest,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from .models.license import LicenseTenantModule, ModuleSource
        from .repositories.license import (LicenseModuleRepository,
                                           LicenseTenantModuleRepository)

        tenant_id = _current_tenant(current_user)
        async with db.session() as session:
            await _require_manage(current_user, tenant_id)
            module = await LicenseModuleRepository(session).get_by_code(code)
            if module is None:
                raise HTTPException(status_code=404, detail="Module introuvable au catalogue")

        pay = _pay()
        try:
            status_ = await pay.verify(body.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("xpay verify impossible (%s) : %s", body.session_id, exc)
            raise HTTPException(status_code=502, detail="Paiement invérifiable auprès du prestataire")

        meta = status_.metadata or {}
        # L'APPARTENANCE d'abord : une session qui n'est pas la vôtre ne doit pas même révéler si elle est
        # payée. Ces métadonnées viennent du serveur (posées à l'ouverture de la caisse), jamais du navigateur.
        if meta.get("tenant_id") != tenant_id or meta.get("module_code") != code:
            raise HTTPException(status_code=403, detail="Cette session de paiement n'est pas la vôtre")
        if not status_.paid:
            raise HTTPException(status_code=402, detail={"code": "not_paid", "module": code})

        # L'abonnement vient de la session elle-même quand le prestataire le donne ; sinon on le cherche
        # par l'adresse du client. Son absence n'empêche pas d'activer ce qui est payé : le webhook le
        # posera au premier renouvellement.
        subscription_id = meta.get("subscription_id")
        if not subscription_id:
            try:
                subscription_id = (await pay.subscription(_payer_email(current_user))).subscription_id
            except Exception as exc:  # noqa: BLE001
                logger.info("abonnement non relu (%s/%s) : %s", tenant_id, code, exc)

        async with db.session() as session:
            repo = LicenseTenantModuleRepository(session)
            row = await repo.get_one(tenant_id, code)
            if row is None:
                row = LicenseTenantModule(
                    tenant_id=tenant_id, module_code=code, source=ModuleSource.PAID
                )
                await repo.save(row)
            row.source = ModuleSource.PAID
            row.expires_at = None  # abonnement en cours : pas d'échéance connue
            if subscription_id:
                row.stripe_item_id = subscription_id
            await _svc(session).invalidate_tenant(tenant_id)
            await session.commit()
        return {"code": code, "taken": True, "source": ModuleSource.PAID.value}

    @router.post("/me/billing/portal", summary="Gérer ses abonnements chez le prestataire")
    async def billing_portal(
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        tenant_id = _current_tenant(current_user)
        await _require_manage(current_user, tenant_id)
        try:
            url = await _pay().portal(_payer_email(current_user))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("portail de facturation indisponible (%s) : %s", tenant_id, exc)
            raise HTTPException(status_code=502, detail="Portail de facturation indisponible")
        return {"url": url}

    @router.post("/webhook", summary="Événement du prestataire de paiement (public, signé)")
    async def payment_webhook(request: Request) -> dict:
        """Renouvellement, résiliation, suspension : ce que l'organisation ne nous dit pas.

        Route PUBLIQUE par nécessité — c'est le prestataire qui appelle, sans jeton. Ce qui la
        garde n'est pas une permission mais la SIGNATURE du corps : sans elle, ou fausse, on
        refuse et on n'écrit rien. À exclure du gate de licence (voir `plugin.yaml`), sinon un
        tenant impayé ne pourrait plus recevoir l'événement qui le remettrait en règle.

        Idempotente : le prestataire réessaie, et chaque réaction est une mise à l'état voulu, pas
        un incrément.
        """
        from .repositories.license import LicenseTenantModuleRepository
        from .services.webhook import decide

        payload = await request.body()
        signature = request.headers.get("stripe-signature") or request.headers.get("Stripe-Signature")
        if not signature:
            raise HTTPException(status_code=400, detail="Signature absente")
        try:
            event = _pay().webhook(payload, signature)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - signature fausse ou secret absent : on refuse
            logger.warning("webhook de paiement refusé : %s", exc)
            raise HTTPException(status_code=400, detail="Signature invalide")

        plan = decide(event)
        if plan.action == "ignore" or not plan.targets_a_module:
            logger.info("webhook %s : sans effet sur l'entitlement", plan.reason)
            return {"handled": False, "reason": plan.reason}

        async with db.session() as session:
            repo = LicenseTenantModuleRepository(session)
            row = None
            if plan.tenant_id and plan.module_code:
                row = await repo.get_one(plan.tenant_id, plan.module_code)
            if row is None and plan.subscription_id:
                # Pas de métadonnées : l'abonnement retenu à la confirmation sert de filet.
                row = await repo.by_subscription(plan.subscription_id)
            if row is None:
                logger.info("webhook %s : aucun module correspondant", plan.reason)
                return {"handled": False, "reason": "module_inconnu"}

            if plan.action == "keep":
                row.expires_at = None
                if plan.subscription_id and not row.stripe_item_id:
                    row.stripe_item_id = plan.subscription_id
            elif plan.action == "until":
                row.expires_at = plan.until
            elif plan.action == "end_now":
                row.expires_at = datetime.now(tz=timezone.utc)
            await _svc(session).invalidate_tenant(row.tenant_id)
            await session.commit()
            tenant, code = row.tenant_id, row.module_code

        logger.info("webhook %s : %s/%s -> %s", plan.reason, tenant, code, plan.action)
        return {"handled": True, "tenant_id": tenant, "module": code, "action": plan.action}

    # ── Plans ─────────────────────────────────────────────────────────────────

    @router.post(
        "/plans",
        status_code=status.HTTP_201_CREATED,
        summary="Créer un plan",
    )
    async def create_plan(
        body: PlanCreateRequest,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from .models.license import LicensePlan, LicenseType

        _require_platform_admin(current_user)

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

    # ── Catalogue des modules ─────────────────────────────────────────────────
    #
    # Ce que les modules SONT et d'où ils s'installent est l'affaire du registre d'xcore et de sa
    # marketplace. Ici, uniquement la couche commerciale : gratuit ou payant, et à quel prix.
    # Lire le catalogue est ouvert à tout membre identifié (l'écran « marketplace » d'une
    # organisation en a besoin) ; le modifier est réservé à l'administration de la plateforme — un
    # prix est partagé par toutes les organisations.

    @router.get("/modules", summary="Catalogue des modules (gratuits et payants)")
    async def list_modules(
        include_inactive: bool = False,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> list:
        from .repositories.license import LicenseModuleRepository

        # Un module retiré du rayon ne se montre qu'à l'administration de la plateforme.
        if include_inactive:
            _require_platform_admin(current_user)
        async with db.session() as session:
            repo = LicenseModuleRepository(session)
            return [
                _module_to_dict(m)
                for m in await repo.in_catalog(only_active=not include_inactive)
            ]

    @router.put("/modules/{code}", summary="Mettre un module en rayon / changer son prix (admin)")
    async def upsert_module(
        code: str,
        body: ModuleUpsertRequest,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from .models.license import BillingPeriod, LicenseModule
        from .repositories.license import LicenseModuleRepository

        _require_platform_admin(current_user)
        if body.code != code:
            raise HTTPException(
                status_code=400, detail="Le code du corps et celui du chemin diffèrent"
            )
        async with db.session() as session:
            repo = LicenseModuleRepository(session)
            module = await repo.get_by_code(code)
            if module is None:
                module = LicenseModule(code=code, label=body.label or code)
                await repo.save(module)

            changes = body.model_dump(exclude_unset=True, exclude={"code"})
            if "period" in changes and changes["period"] is not None:
                changes["period"] = BillingPeriod(changes["period"])
            for field, value in changes.items():
                if value is None and field in ("label", "free", "price", "currency", "period", "active"):
                    continue  # ces champs n'acceptent pas null : on garde la valeur en place
                setattr(module, field, value)

            # Un module payant sans tarif Stripe serait proposé puis impossible à encaisser.
            if not module.free and not module.stripe_price_id:
                raise HTTPException(
                    status_code=400,
                    detail="Un module payant exige son tarif Stripe (stripe_price_id)",
                )
            if module.free:
                module.price = 0.0
            await session.commit()
            return _module_to_dict(module)

    @router.patch(
        "/plans/{plan_id}",
        summary="Mettre à jour le mapping Stripe d'un plan (admin)",
    )
    async def update_plan(
        plan_id: str,
        body: PlanUpdateRequest,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> dict:
        from uuid import UUID

        # Un plan est partagé par toutes les entreprises : réservé à
        # l'administration de la plateforme, jamais au propriétaire d'un tenant.
        _require_platform_admin(current_user)
        try:
            pid = UUID(str(plan_id))
        except ValueError:
            raise HTTPException(status_code=404, detail="Plan introuvable")

        async with db.session() as session:
            repo = LicensePlanRepository(session)
            plan = await repo.get(pid)
            if plan is None:
                raise HTTPException(status_code=404, detail="Plan introuvable")
            for field, value in body.model_dump(exclude_unset=True).items():
                # Seuls les identifiants Stripe et la description acceptent null.
                if value is None and field in ("price", "max_users", "max_machines"):
                    continue
                setattr(plan, field, value)
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
            has_access, can_manage = await _tenant_permissions(current_user, tenant_id)
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            licenses = await _svc(session).get_for_tenant(tenant_id)
        if not can_manage:
            licenses = [_mask_key(lic) for lic in licenses]
        return [lic.model_dump() for lic in licenses]

    @router.get("/licenses/{license_id}", response_model=LicenseResponse)
    async def get_license(
        license_id: str,
        current_user: AuthPayload = Depends(get_current_user),
    ) -> LicenseResponse:
        async with db.session() as session:
            _, has_access, can_manage = await _license_permissions(
                session, current_user, license_id
            )
            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")
            result = await _svc(session).get_by_id(license_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Licence non trouvée")
        return result if can_manage else _mask_key(result)

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
        _require_platform_admin(current_user)
        async with db.session() as session:
            try:
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
        _require_platform_admin(current_user)
        async with db.session() as session:
            try:
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

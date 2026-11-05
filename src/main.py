from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
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
from .schema_upgrade import ensure_schema_sync
from .services.events import XLicenseEvents
from .services.jwt import (
    DegradedTokenService,
    LicenseKeyError,
    LicenseTokenService,
    resolve_key_paths,
)
from .services.license import LicenseService
from .services.seed import (PLATFORM_PLUGINS, seed_license_plans,
                            sync_license_modules)
from .state_machine import LicenseStateMachineError

logger = logging.getLogger("xlicense.plugin")

# La racine du plugin : ses DocTypes et son espace de travail vivent à côté de `src/`.
BASE = Path(__file__).resolve().parent.parent

# Durée de l'essai posé à la création d'une organisation. L'essai porte sur TOUS les modules
# (décision du 3 nov. 2026) : le plan retenu est celui qui ouvre `["*"]`.
TRIAL_DAYS = 14


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

    def _config(self) -> dict:
        """La configuration du plugin, telle que le manifeste l'écrit.

        xcore met dans `ctx.config` TOUTES les clés inconnues du manifeste (`extra`), donc un bloc `config:`
        y arrive IMBRIQUÉ : `ctx.config["config"]["enforce"]`. Le reste du plugin — middleware compris — lit
        ces clés à plat, et ne lisait donc rien du tout. On déplie ici, en acceptant les deux écritures :
        un manifeste qui pose ses clés au premier niveau continue de marcher.
        """
        raw = self.ctx.config or {}
        inner = raw.get("config")
        return {**raw, **inner} if isinstance(inner, dict) else raw

    async def _initialize(self, db, stripe_map: dict | None = None) -> None:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            changes = await conn.run_sync(ensure_schema_sync)
        if changes:
            logger.info("[xlicense] schéma mis à niveau : %s", changes)

        await seed_license_plans(db.session(), stripe_map=stripe_map)

    async def _mount_doctypes(self, db) -> None:
        """Expose le catalogue, les modules de l'organisation et sa licence comme DocTypes du Desk.

        Couplage SOUPLE, et c'est une décision : xlicense garde la plateforme (son middleware de licence tourne
        pour toutes les routes) et ne doit pas dépendre du moteur de DocTypes pour démarrer. Sans le moteur, il
        reste une API ; avec lui, il gagne ses écrans sans qu'on en écrive un seul (le motif `iam`).

        Les documents ne sont pas copiés : ils restent dans les tables de xlicense, lus et écrits par la source
        `xlicense` (`src/doctypes.py`). Le moteur applique les grants, le schéma strict, les filtres et le tri ;
        xlicense reste juge de ce qu'il accepte d'écrire.
        """
        registry = self.get_service("doctype_registry")
        sources = self.get_service("doctype_sources")
        if registry is None or sources is None:
            logger.info("[xlicense] moteur de DocTypes absent : pas d'écrans, l'API suffit")
            return

        from .doctypes import XlicenseSource

        me = self.ctx.name
        token_service, cache, events = self._token_service, self._cache, self._events

        async def forget(session, tenant_id: str) -> None:
            await LicenseService(
                token_service, LicenseRepository(session), cache_service=cache, events=events
            ).invalidate_tenant(tenant_id)

        # La source AVANT les DocTypes : ils la citent par son nom.
        sources.register(me, me, XlicenseSource(db.session, forget))
        registry.load_dir(me, BASE / "doctypes")
        registry.load_workspaces(me, BASE / "workspaces")
        grants = self.get_service("doctype_grants").for_plugin(me)
        # Les grants délégables partent déjà avec `default_roles: [tenant_admin]` (`GrantSpec.as_declaration`) :
        # l'administrateur d'une organisation voit donc ses modules et sa licence sans qu'un admin plateforme
        # intervienne. `xlicense:module:write` n'est PAS délégable : fixer un prix reste à la plateforme.
        await self.ctx.events.emit("rbac.declare", {"plugin": me, "grants": grants}, source=me)
        logger.info("[xlicense] DocTypes montés (%d grant(s) déclaré(s))", len(grants))

    async def _unmount_doctypes(self) -> None:
        for service in ("doctype_registry", "doctype_sources"):
            try:
                owner = self.get_service(service)
                if owner is not None:
                    owner.unregister_owner(self.ctx.name)
            except Exception as exc:  # noqa: BLE001 - un déchargement ne doit jamais empêcher l'arrêt
                logger.warning("[xlicense] %s.unregister_owner : %s", service, exc)

    async def _sync_catalog(self, db) -> None:
        """Aligne le catalogue sur les plugins installés dans CETTE instance (marketplace interne).

        La marketplace est dans l'application et on facture les plugins de l'instance, façon Odoo :
        le registre local (`ctx.registry`) est la liste de ce qui existe ici. Un nouveau plugin
        arrive gratuit et hors de prix ; mettre en vente reste un geste de l'administrateur.

        Jamais bloquant : sans registre (contexte réduit, test), le catalogue reste ce qu'il est et
        l'administrateur peut toujours poser un module à la main par `PUT /modules/{code}`.
        """
        registry = getattr(self.ctx, "registry", None) if self.ctx else None
        if registry is None:
            logger.info("[xlicense] pas de registre de plugins : catalogue inchangé")
            return
        not_sellable = frozenset(self._config().get("not_sellable") or PLATFORM_PLUGINS)
        try:
            installed = registry.all_plugins()
        except Exception as exc:  # noqa: BLE001 - un catalogue non synchronisé ne doit pas empêcher le démarrage
            logger.warning("[xlicense] registre illisible (%s) : catalogue inchangé", exc)
            return
        async with db.session() as session:
            created, retired = await sync_license_modules(
                session, installed, not_sellable=not_sellable, pricing=self._pricing()
            )
            await session.commit()
        if created or retired:
            logger.info(
                "[xlicense] catalogue synchronisé : %d module(s) ajouté(s), %d retiré(s) du rayon",
                created,
                retired,
            )
        await self._refresh_prices(db)

    async def _refresh_prices(self, db) -> None:
        """Aligne le montant affiché sur ce que le prestataire facturera VRAIMENT.

        C'est la seule chose que la règle « on ne réécrit pas un module en rayon » doit céder, et le
        pourquoi est net : l'administrateur décide QUEL tarif s'applique (`stripe_price_id`), le
        prestataire décide COMBIEN il prélève. Les garder séparés, c'est afficher 19,99 € et débiter
        1 000 € — mesuré, le 3 nov. 2026, avec des identifiants de tarifs de plans réutilisés pour des
        modules. L'argent a une seule autorité.

        Jamais bloquant : sans `ext.xpay`, ou si un tarif est illisible, le catalogue reste tel quel et
        l'écart est JOURNALISÉ plutôt que subi en silence.
        """
        pay = self.get_service("ext.xpay")
        if pay is None or not hasattr(pay, "price"):
            return
        from .repositories.license import LicenseModuleRepository

        async with db.session() as session:
            repo = LicenseModuleRepository(session)
            changed = 0
            for module in await repo.in_catalog(only_active=False):
                if module.free or not module.stripe_price_id:
                    continue
                try:
                    tarif = await pay.price(module.stripe_price_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[xlicense] tarif %s illisible (%s) : « %s » garde %s %s",
                        module.stripe_price_id, exc, module.code, module.price, module.currency,
                    )
                    continue
                if not tarif.get("recurring"):
                    logger.error(
                        "[xlicense] « %s » vise un tarif NON récurrent : l'abonnement sera refusé", module.code
                    )
                amount, currency = tarif.get("amount"), tarif.get("currency")
                if amount is None or currency is None:
                    continue
                if float(module.price or 0) != float(amount) or module.currency != currency:
                    logger.warning(
                        "[xlicense] « %s » : %s %s annoncé, %s %s facturé — on suit le prestataire",
                        module.code, module.price, module.currency, amount, currency,
                    )
                    module.price, module.currency = float(amount), currency
                    changed += 1
                interval = tarif.get("interval")
                if interval and (module.period.value if module.period else None) != interval:
                    from .models.license import BillingPeriod

                    try:
                        module.period = BillingPeriod(interval)
                        changed += 1
                    except ValueError:
                        pass
            if changed:
                await session.commit()
                logger.info("[xlicense] %d tarif(s) réalignés sur le prestataire", changed)

    def _pricing(self) -> dict[str, dict]:
        """Le tarif de DÉPART des modules, déclaré par l'opérateur dans `config.paid_modules`.

        `price_env` nomme la variable qui porte l'identifiant de tarif chez le prestataire, plutôt que de le
        coller dans le manifeste : les tarifs vivent déjà dans le bloc `env:` de l'hôte, et un identifiant y
        change sans toucher à la configuration du plugin.

        N'est appliqué qu'à la CRÉATION d'un module au catalogue : une fois en rayon, c'est l'administrateur de
        la plateforme qui décide, et aucun redémarrage ne doit défaire son prix.
        """
        wanted = self._config().get("paid_modules") or {}
        if not isinstance(wanted, dict):
            logger.warning("[xlicense] config.paid_modules ignoré : un objet est attendu")
            return {}
        env = self.ctx.env or {}
        out: dict[str, dict] = {}
        for code, raw in wanted.items():
            spec = dict(raw or {}) if isinstance(raw, dict) else {}
            key = spec.pop("price_env", None)
            price_id = (env.get(key) if key else spec.get("stripe_price_id")) or ""
            if not price_id:
                logger.warning(
                    "[xlicense] module « %s » laissé GRATUIT : tarif introuvable (%s)", code, key or "stripe_price_id"
                )
                continue
            out[str(code)] = {**spec, "stripe_price_id": price_id}
        return out

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

        # Token service : une clé absente ne doit pas empêcher le chargement (ni celui des autres
        # plugins) → état DÉGRADÉ, routes de licence en 503 explicite.
        self._degraded_reason: str | None = None
        try:
            private_path, public_path = resolve_key_paths(env)
            self._token_service = LicenseTokenService(
                private_key_path=private_path, public_key_path=public_path
            )
        except LicenseKeyError as exc:
            self._degraded_reason = str(exc)
            logger.error("[xlicense] DÉGRADÉ : %s", self._degraded_reason)
            self._token_service = DegradedTokenService(self._degraded_reason)
            self._events = None
            self.app = self._degraded_router(self._degraded_reason)
            self._register_health()
            return
        self._register_health()

        # Events — ext.websocket optionnel (absent dans un déploiement qui ne
        # le déclare pas, ne doit pas empêcher le chargement du plugin).
        ws = None
        try:
            ws = self.get_service("ext.websocket")
        except Exception as exc:
            logger.warning("[xlicense] ext.websocket indisponible : %s", exc)
        self._events = XLicenseEvents(self.ctx.events, ws=ws)

        # Router
        self.app = build_router(
            db,
            cache,
            self._token_service,
            caller=self.call_plugin,
            events=self._events,
            # Le paiement passe par l'extension `ext.xpay` : on passe le résolveur de services
            # plutôt que le service, pour que son absence soit un 503 explicite à l'appel et non
            # un refus de démarrage de tout le plugin.
            get_service=self.get_service,
            config=self._config(),
        )

        # Les écrans : le catalogue, les modules de l'organisation et sa licence comme DocTypes du Desk.
        # Jamais bloquant — sans le moteur, xlicense reste une API (voir `_mount_doctypes`).
        try:
            await self._mount_doctypes(db)
        except Exception as exc:  # noqa: BLE001 - des écrans en moins, pas un plugin de licence en panne
            logger.error("[xlicense] DocTypes non montés : %s", exc)

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

        # Le catalogue se synchronise quand TOUT est chargé, pas au chargement de xlicense : il démarre avant
        # les plugins métier, et `registry.all_plugins()` ne verrait alors que la plateforme — zéro module en
        # rayon. xcore émet cet événement une fois le parc complet.
        @self.ctx.events.on("xcore.plugins.booted")
        async def _on_booted(event: Event) -> None:
            try:
                await self._sync_catalog(self._db)
            except Exception as exc:  # noqa: BLE001 - un catalogue en retard ne doit pas faire tomber le démarrage
                logger.error("[xlicense] catalogue non synchronisé : %s", exc)

        @self.ctx.events.on("xlicense.config")
        async def _on_config(event: Event) -> dict:
            # Dépliée : sans cela le middleware ne lisait ni `enforce` ni `excluded_prefixes`.
            return self._config()

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
                    # L'essai porte sur TOUS les modules : on prend un plan qui les ouvre (`["*"]`),
                    # pas le premier venu — `plans[0]` donnait l'essai du plan le plus pauvre.
                    plan = next(
                        (p for p in plans if "*" in (p.modules or [])),
                        plans[0] if plans else None,
                    )
                if plan is None:
                    return

                await svc.create_license(
                    LisenseCreate(
                        tenant_id=tenant_id,
                        plan_id=str(plan.id),
                        state="trial",
                        expires_at=TRIAL_DAYS,
                        extra={"source": "tenant_onboarding"},
                    )
                )
                await session.commit()

    @staticmethod
    def _degraded_router(reason: str) -> APIRouter:
        """Toutes les routes de licence répondent 503 avec la cause et la marche à suivre."""
        router = APIRouter()

        @router.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        async def _indisponible(path: str):
            raise HTTPException(status_code=503, detail=f"Service de licences indisponible : {reason}")

        return router

    def _register_health(self) -> None:
        health = getattr(self.ctx, "health", None)
        if health is None or not hasattr(health, "register"):
            return

        @health.register("xlicense.keys")
        async def _check_keys():
            if self._degraded_reason:
                return False, self._degraded_reason
            return True, "clés de licence chargées"

    async def on_unload(self) -> None:
        # Les DocTypes et la source partent avec le plugin : le moteur ne doit pas garder un type dont la
        # source a disparu (il répondrait 503 à chaque lecture).
        await self._unmount_doctypes()

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

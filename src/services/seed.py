from sqlalchemy.ext.asyncio import AsyncSession

from ..models.license import (BillingPeriod, LicenseModule, LicensePlan,
                              LicenseType)
from ..repositories.license import (LicenseModuleRepository,
                                    LicensePlanRepository)


# Modules (segments de route) inclus par plan. ["*"] = tous les modules.
# Les noms doivent correspondre au 1er segment de route après le plugin_prefix
# (ex: /app/<module>/...). auth & xlicense sont toujours accessibles (exclus du gate).
_PLAN_MODULES: dict[str, list[str]] = {
    "Basic Plan": ["xcompany", "tasks"],
    "Pro Plan": ["xcompany", "tasks", "xform", "xwms", "xfinance", "xaudit"],
    "Enterprise Plan": ["*"],
    "Lifetime Plan": ["*"],
}


# Les plugins qui ne sont PAS des modules vendables : l'infrastructure du SaaS. On ne facture pas
# l'authentification, la facturation elle-même, les notifications ni le moteur de DocTypes — ils
# sont la plateforme. Surchargeable par `config.not_sellable` du manifeste.
PLATFORM_PLUGINS: frozenset[str] = frozenset(
    {"auth", "xauth", "xlicense", "xpay", "xpayproxy", "xpulse", "xcore-doctype", "system", "iam"}
)


async def sync_license_modules(
    session: AsyncSession,
    installed: list[dict],
    *,
    not_sellable: frozenset[str] = PLATFORM_PLUGINS,
    pricing: dict[str, dict] | None = None,
) -> tuple[int, int]:
    """Aligne le catalogue sur les plugins INSTALLÉS dans cette instance (façon Odoo).

    La marketplace est dans l'application : ce qu'on vend, ce sont les plugins présents dans
    l'instance, et le registre local (`ctx.registry.all_plugins()`) en est la liste. On en reprend
    le nom, la description et la version pour la tuile.

    Deux règles, et elles comptent :
      • un module DÉJÀ en rayon n'est jamais réécrit — le prix et le drapeau gratuit/payant sont
        la décision de l'administrateur de la plateforme, qu'un démarrage ne doit pas défaire
        (seule la description, purement informative, est rafraîchie) ;
      • un nouveau module arrive GRATUIT : mettre en vente est un acte explicite, jamais un effet
        de bord d'une installation.

    Un plugin disparu n'est pas supprimé : des organisations l'ont peut-être payé, et son
    historique de facturation doit rester lisible. Il est seulement retiré du rayon (`active`).
    Rend (créés, retirés du rayon).
    """
    repo = LicenseModuleRepository(session)
    seen: set[str] = set()
    created = 0

    for entry in installed:
        code = (entry.get("name") or "").strip()
        if not code or code in not_sellable:
            continue
        seen.add(code)
        existing = await repo.get_by_code(code)
        description = (entry.get("description") or "").strip()[:1024] or None
        if existing is not None:
            if description and existing.description != description:
                existing.description = description  # informatif : jamais le prix
            if not existing.active:
                existing.active = True  # réinstallé : il revient en rayon tel qu'il était
            continue
        # Le tarif de départ, s'il est déclaré pour ce module. Appliqué À LA CRÉATION seulement : au prochain
        # démarrage la règle du dessus reprend la main et le prix de l'administrateur survit.
        wanted = (pricing or {}).get(code) or {}
        module = LicenseModule(
            code=code,
            label=wanted.get("label") or code.replace("_", " ").replace("-", " ").title(),
            description=description,
            free=True,
            price=0.0,
        )
        price_id = (wanted.get("stripe_price_id") or "").strip()
        if price_id:
            module.free = False
            module.price = float(wanted.get("price") or 0)
            module.currency = (wanted.get("currency") or "EUR")[:3].upper()
            if wanted.get("period"):
                module.period = BillingPeriod(str(wanted["period"]))
            module.stripe_price_id = price_id
        await repo.save(module)
        created += 1

    retired = 0
    for module in await repo.in_catalog(only_active=True):
        if module.code not in seen:
            module.active = False
            retired += 1
    return created, retired


async def _backfill_modules(repo: LicensePlanRepository) -> None:
    """Renseigne `modules` sur des plans existants dont la liste est vide.

    Idempotent : ne touche pas un plan qui a déjà des modules (personnalisation
    via PATCH /plans/{id} préservée).
    """
    for name, modules in _PLAN_MODULES.items():
        plan = await repo.get_by_name(name)
        if plan is None:
            continue
        if not (plan.modules or []):
            plan.modules = list(modules)
            await repo.save(plan)


async def _backfill_stripe_map(
    repo: LicensePlanRepository,
    stripe_map: dict[str, dict],
) -> None:
    """
    Renseigne le mapping Stripe sur des plans déjà existants.

    Ne touche qu'aux ids absents (None/"") afin de ne jamais écraser une valeur
    posée manuellement via PATCH /plans/{id}.
    """
    for name, mapping in stripe_map.items():
        plan = await repo.get_by_name(name)
        if plan is None:
            continue
        price_id = (mapping or {}).get("price_id")
        product_id = (mapping or {}).get("product_id")
        changed = False
        if price_id and not plan.stripe_price_id:
            plan.stripe_price_id = price_id
            changed = True
        if product_id and not plan.stripe_product_id:
            plan.stripe_product_id = product_id
            changed = True
        if changed:
            await repo.save(plan)


async def seed_license_plans(
    session: AsyncSession,
    stripe_map: dict[str, dict] | None = None,
):
    """
    Seed des plans de licence.

    stripe_map : mapping optionnel {nom_du_plan: {"price_id": ..., "product_id": ...}}
    pour pré-remplir le lien vers le catalogue Stripe. Les ids manquants restent
    None et peuvent être renseignés plus tard via PATCH /plans/{id}.
    """
    stripe_map = stripe_map or {}

    async with session as s:
        repo = LicensePlanRepository(session=s)
        if await repo.get_by_name("Basic Plan"):
            # Plans déjà créés : on ne recrée rien, mais on (re)pose le mapping
            # Stripe fourni pour combler les ids manquants (ex. ajoutés en env
            # après le premier seed). On n'écrase jamais un id déjà renseigné.
            await _backfill_stripe_map(repo, stripe_map)
            await _backfill_modules(repo)
            await s.commit()
            return

        plans = [
            LicensePlan(
                name="Basic Plan",
                type=LicenseType.STARTER,
                price=9.99,
                max_users=5,
                max_machines=5,
                description="A basic license plan with limited features.",
                features={
                    "api_access": True,
                    "custom_domain": False,
                    "sso": False,
                    "audit_logs": False,
                    "priority_support": False,
                },
                quotas={
                    "max_requests_per_day": 1_000,
                    "storage_gb": 5,
                    "webhooks": 0,
                },
            ),
            LicensePlan(
                name="Pro Plan",
                type=LicenseType.PRO,
                price=19.99,
                max_users=10,
                max_machines=10,
                description="A pro license plan with additional features.",
                features={
                    "api_access": True,
                    "custom_domain": True,
                    "sso": False,
                    "audit_logs": True,
                    "priority_support": False,
                },
                quotas={
                    "max_requests_per_day": 10_000,
                    "storage_gb": 50,
                    "webhooks": 5,
                },
            ),
            LicensePlan(
                name="Enterprise Plan",
                type=LicenseType.ENTERPRISE,
                price=49.99,
                max_users=100,
                max_machines=100,
                description="An enterprise license plan with all features.",
                features={
                    "api_access": True,
                    "custom_domain": True,
                    "sso": True,
                    "audit_logs": True,
                    "priority_support": True,
                },
                quotas={
                    "max_requests_per_day": 100_000,
                    "storage_gb": 500,
                    "webhooks": 50,
                },
            ),
            LicensePlan(
                name="Lifetime Plan",
                type=LicenseType.LIFETIME,
                price=199.99,
                max_users=1000,
                max_machines=1000,
                description="A lifetime license plan with unlimited features.",
                features={
                    "api_access": True,
                    "custom_domain": True,
                    "sso": True,
                    "audit_logs": True,
                    "priority_support": True,
                },
                quotas={
                    "max_requests_per_day": -1,
                    "storage_gb": -1,
                    "webhooks": -1,
                },
            ),
        ]
        for plan in plans:
            mapping = stripe_map.get(plan.name) or {}
            plan.stripe_price_id = mapping.get("price_id")
            plan.stripe_product_id = mapping.get("product_id")
            plan.modules = list(_PLAN_MODULES.get(plan.name, []))
            await repo.save(plan)
        await s.commit()

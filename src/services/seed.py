from sqlalchemy.ext.asyncio import AsyncSession

from ..models.license import LicensePlan, LicenseType
from ..repositories.license import LicensePlanRepository


# Modules (segments de route) inclus par plan. ["*"] = tous les modules.
# Les noms doivent correspondre au 1er segment de route après le plugin_prefix
# (ex: /app/<module>/...). auth & xlicense sont toujours accessibles (exclus du gate).
_PLAN_MODULES: dict[str, list[str]] = {
    "Basic Plan": ["xcompany", "tasks"],
    "Pro Plan": ["xcompany", "tasks", "xform", "xwms", "xfinance", "xaudit"],
    "Enterprise Plan": ["*"],
    "Lifetime Plan": ["*"],
}


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

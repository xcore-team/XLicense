from sqlalchemy.ext.asyncio import AsyncSession

from ..models.license import LicensePlan, LicenseType
from ..repositories.license import LicensePlanRepository


async def seed_license_plans(session: AsyncSession):
    async with session as s:
        repo = LicensePlanRepository(session=s)
        if await repo.get_by_name("Basic Plan"):
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
            await repo.save(plan)
        await s.commit()

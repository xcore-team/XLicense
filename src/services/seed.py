from ..repositories.license import LicensePlanRepository
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.license import LicensePlan, LicenseType


async def seed_license_plans(session: AsyncSession):
    async with session as s:
        license_plan_repository = LicensePlanRepository(session=s)
        if await license_plan_repository.get_by_name("Basic Plan"):
            return  # Plans already seeded
        await license_plan_repository.save(
            LicensePlan(
                    name = "Basic Plan",
                    type = LicenseType.STARTER,
                    price = 9.99,
                    max_users = 5,
                    max_machines = 5,
                    description = "A basic license plan with limited features."
            )
        )
        await license_plan_repository.save(
            LicensePlan(
                name="Pro Plan",
                type=LicenseType.PRO,
                price=19.99,
                max_users=10,
                max_machines=10,
                description="A pro license plan with additional features."
            )
        )
        await license_plan_repository.save(
            LicensePlan(
                name="Enterprise Plan",
                type=LicenseType.ENTERPRISE,
                price=49.99,
                max_users=100,
                max_machines=100,
                description="An enterprise license plan with all features."
            )
        )

        await license_plan_repository.save(
            LicensePlan(
                name="Lifetime Plan",
                type=LicenseType.LIFETIME,
                price=199.99,
                max_users=1000,
                max_machines=1000,
                description="A lifetime license plan with unlimited features."
            )
        )
        await s.commit()

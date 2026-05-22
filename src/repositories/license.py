from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.future import select
from sqlalchemy.orm import joinedload

from ..models.license import License, LicensePlan, LicenseState, LicenseType
from .base import BaseRepository


class LicensePlanRepository(BaseRepository[LicensePlan]):
    model = LicensePlan

    async def get_by_name(self, name: str) -> LicensePlan | None:
        result = await self.session.execute(
            select(LicensePlan).where(LicensePlan.name == name)
        )
        return result.scalars().first()


class LicenseRepository(BaseRepository[License]):
    model = License

    async def get_by_license_id(self, license_id: str) -> License | None:
        license_uuid = UUID(str(license_id))
        result = await self.session.execute(
            select(License)
            .options(joinedload(License.plan))
            .where(License.id == license_uuid)
        )
        return result.scalars().first()

    async def update_license_state(
        self, license_id: str, new_state: LicenseState
    ) -> License | None:
        # FIX: suppression du commit() — la transaction appartient au service/route
        license = await self.get_by_license_id(license_id)
        if license:
            license.state = new_state
            await self.session.flush()
            await self.session.refresh(license)
            return license
        return None

    async def update_license_plan(
        self, license_id: str, new_plan: LicensePlan
    ) -> License | None:
        # FIX: suppression du commit()
        license = await self.get_by_license_id(license_id)
        if license:
            license.plan = new_plan
            await self.session.flush()
            await self.session.refresh(license)
            return license
        return None

    async def update_license_type(
        self, license_id: str, new_type: LicenseType
    ) -> License | None:
        # FIX: suppression du commit()
        license = await self.get_by_license_id(license_id)
        if license:
            license.type = new_type
            await self.session.flush()
            await self.session.refresh(license)
            return license
        return None

    async def rotate_key(
        self, license_id: str, new_key: str, new_hash: str
    ) -> License | None:
        # FIX: suppression du commit()
        license = await self.get_by_license_id(license_id)
        if license:
            license.license_key = new_key
            license.license_hash = new_hash
            await self.session.flush()
            await self.session.refresh(license)
            return license
        return None

    async def update_expiration(
        self, license_id: str, new_expiration: datetime | None
    ) -> License | None:
        # FIX: suppression du commit()
        license = await self.get_by_license_id(license_id)
        if license:
            license.expires_at = new_expiration
            await self.session.flush()
            await self.session.refresh(license)
            return license
        return None

    async def validate_license(self, license_id: str) -> bool:
        # FIX: suppression du commit() — flush uniquement
        license = await self.get_by_license_id(license_id)
        if not license:
            return False

        now = datetime.now(tz=timezone.utc)
        if license.expires_at and license.expires_at < now:
            license.state = LicenseState.EXPIRED
            await self.session.flush()
            return False

        if license.state != LicenseState.ACTIVE:
            return False

        license.last_validation_at = now
        await self.session.flush()
        return True

    async def get_all_by(
        self,
        state: LicenseState | None = None,
        type: LicenseType | None = None,
        tenant_id: str | None = None,
    ) -> list[License]:
        # FIX: type hint corrigé — scalars().all() retourne toujours une liste, jamais None
        query = select(License).options(joinedload(License.plan))
        if state:
            query = query.where(License.state == state)
        if type:
            query = query.join(License.plan).where(LicensePlan.type == type)
        if tenant_id:
            query = query.where(License.tenant_id == tenant_id)

        result = await self.session.execute(query)
        return list(result.scalars().all())

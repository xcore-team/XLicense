from __future__ import annotations

from typing import TYPE_CHECKING, Any

LICENSE_CREATED = "xlicense.license.created"
LICENSE_TRANSITIONED = "xlicense.license.transitioned"
LICENSE_RENEWED = "xlicense.license.renewed"
LICENSE_EXPIRED = "xlicense.license.expired"
LICENSE_KEY_ROTATED = "xlicense.license.key_rotated"
LICENSE_PLAN_CHANGED = "xlicense.license.plan_changed"


if TYPE_CHECKING:
    from xcore.kernel.events import EventBus


class XLicenseEvents:
    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus

    async def emit(self, name: str, data: dict[str, Any]) -> None:
        if self._bus is None:
            return
        await self._bus.emit(name, data, source="xlicense")

    async def license_created(
        self,
        license_id: str,
        tenant_id: str,
        plan_id: str,
        state: str,
        expires_at: str | None = None,
    ) -> None:
        await self.emit(LICENSE_CREATED, {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "state": state,
            "expires_at": expires_at,
        })

    async def license_transitioned(
        self,
        license_id: str,
        tenant_id: str,
        from_state: str,
        to_state: str,
        reason: str | None = None,
    ) -> None:
        await self.emit(LICENSE_TRANSITIONED, {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
        })

    async def license_renewed(
        self,
        license_id: str,
        tenant_id: str,
        expires_at: str | None = None,
    ) -> None:
        await self.emit(LICENSE_RENEWED, {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "expires_at": expires_at,
        })

    async def license_expired(
        self,
        license_id: str,
        tenant_id: str,
    ) -> None:
        await self.emit(LICENSE_EXPIRED, {
            "license_id": license_id,
            "tenant_id": tenant_id,
        })

    async def license_key_rotated(
        self,
        license_id: str,
        tenant_id: str,
    ) -> None:
        await self.emit(LICENSE_KEY_ROTATED, {
            "license_id": license_id,
            "tenant_id": tenant_id,
        })

    async def license_plan_changed(
        self,
        license_id: str,
        tenant_id: str,
        plan_id: str,
        reason: str | None = None,
    ) -> None:
        await self.emit(LICENSE_PLAN_CHANGED, {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "reason": reason,
        })

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
    def __init__(self, bus: "EventBus", ws: Any = None) -> None:
        self._bus = bus
        self._ws = ws

    async def emit(self, name: str, data: dict[str, Any]) -> None:
        if self._bus is None:
            return
        await self._bus.emit(name, data, source="xlicense")

    async def _ws_broadcast(self, event: str, data: dict[str, Any]) -> None:
        """Diffusion best-effort sur le canal 'xlicense' — un changement
        d'état de licence (suspension, réactivation, changement de plan)
        doit apparaître en temps réel côté client, pas seulement au prochain
        rechargement de page (même garanties que xcompany/xstock : jamais
        bloquant, jamais d'exception propagée à l'appelant)."""
        if self._ws is None:
            return
        try:
            await self._ws.broadcast(channel="xlicense", event=event, data=data)
        except Exception:
            pass

    async def license_created(
        self,
        license_id: str,
        tenant_id: str,
        plan_id: str,
        state: str,
        expires_at: str | None = None,
    ) -> None:
        data = {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "state": state,
            "expires_at": expires_at,
        }
        await self.emit(LICENSE_CREATED, data)
        await self._ws_broadcast("LICENSE_CREATED", data)

    async def license_transitioned(
        self,
        license_id: str,
        tenant_id: str,
        from_state: str,
        to_state: str,
        reason: str | None = None,
    ) -> None:
        data = {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
        }
        await self.emit(LICENSE_TRANSITIONED, data)
        await self._ws_broadcast("LICENSE_TRANSITIONED", data)

    async def license_renewed(
        self,
        license_id: str,
        tenant_id: str,
        expires_at: str | None = None,
    ) -> None:
        data = {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "expires_at": expires_at,
        }
        await self.emit(LICENSE_RENEWED, data)
        await self._ws_broadcast("LICENSE_RENEWED", data)

    async def license_expired(
        self,
        license_id: str,
        tenant_id: str,
    ) -> None:
        data = {
            "license_id": license_id,
            "tenant_id": tenant_id,
        }
        await self.emit(LICENSE_EXPIRED, data)
        await self._ws_broadcast("LICENSE_EXPIRED", data)

    async def license_key_rotated(
        self,
        license_id: str,
        tenant_id: str,
    ) -> None:
        data = {
            "license_id": license_id,
            "tenant_id": tenant_id,
        }
        await self.emit(LICENSE_KEY_ROTATED, data)
        await self._ws_broadcast("LICENSE_KEY_ROTATED", data)

    async def license_plan_changed(
        self,
        license_id: str,
        tenant_id: str,
        plan_id: str,
        reason: str | None = None,
    ) -> None:
        data = {
            "license_id": license_id,
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "reason": reason,
        }
        await self.emit(LICENSE_PLAN_CHANGED, data)
        await self._ws_broadcast("LICENSE_PLAN_CHANGED", data)

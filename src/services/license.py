from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from ..models.license import License, LicenseState
from ..repositories.license import LicensePlanRepository, LicenseRepository
from ..schemas import LicenseResponse, LisenseCreate
from ..services.events import XLicenseEvents
from ..services.jwt import LicenseTokenService
from ..state_machine import LicenseStateMachine, LicenseStateMachineError

if TYPE_CHECKING:
    from xcore.services.cache import CacheService

logger = logging.getLogger("xlicense.service")

_CACHE_KEY_ACTIVE = "license:{tenant_id}:active"
_CACHE_KEY_BY_ID = "license:id:{license_id}"
_CACHE_TTL = 300  # 5 min


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class LicenseService:
    """
    Service de gestion du cycle de vie des licences.

    Responsabilités :
        - Création, rotation de clé, renouvellement
        - Transitions d'état via LicenseStateMachine
        - Cache-aside (xcore CacheService)
        - Validation (JWT + état + expiration)
    """

    def __init__(
        self,
        token_service: LicenseTokenService,
        license_repository: LicenseRepository,
        plan_repository: LicensePlanRepository | None = None,
        cache_service: "CacheService | None" = None,
        events: XLicenseEvents | None = None,
    ) -> None:
        self._token = token_service
        self._repo = license_repository
        self._plan_repo = plan_repository
        self._cache = cache_service
        self._events = events

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create_license(self, data: LisenseCreate) -> LicenseResponse:
        license_uuid = uuid4()
        license_id = str(license_uuid)
        now = datetime.now(tz=timezone.utc)
        state_value = (data.state or LicenseState.TRIAL.value).lower()
        state = LicenseState(state_value)
        plan_uuid = UUID(str(data.plan_id))

        expires_at = now + \
            timedelta(days=data.expires_at) if data.expires_at else None

        license_key = self._token.create_license_token(
            tenant_id=data.tenant_id,
            license_id=license_id,
            state=state.value,
            expires_in_days=data.expires_at or 30,
            extra=getattr(data, "extra", None),  # type: ignore
        )

        obj = License(
            id=license_uuid,
            tenant_id=data.tenant_id,
            plan_id=plan_uuid,
            state=state,
            expires_at=expires_at,
            license_key=license_key,
            license_hash=self._token.hash_token(license_key),
        )

        created = await self._repo.save(obj)
        response = _to_response(created)

        await self._cache_set_active(data.tenant_id, created)
        await self._cache_set_by_id(license_id, created)

        logger.info(
            "Licence créée [%s] tenant=%s plan=%s expires=%s",
            license_id,
            data.tenant_id,
            data.plan_id,
            expires_at,
        )

        if self._events:
            await self._events.license_created(
                license_id=license_id,
                tenant_id=data.tenant_id,
                plan_id=str(data.plan_id),
                state=state.value,
                expires_at=expires_at.isoformat() if expires_at else None,
            )

        return response

    # ── State transitions ─────────────────────────────────────────────────────

    async def transition(
        self,
        license_id: str,
        to: LicenseState,
        reason: str | None = None,
    ) -> LicenseResponse:
        """
        Apply a state transition. Persists and invalidates cache.

        Example:
            await svc.transition(lic_id, LicenseState.SUSPENDED, reason="Impayé")
        """
        lic = await self._repo.get_by_license_id(license_id)
        if lic is None:
            raise ValueError(f"Licence introuvable : {license_id}")

        sm = LicenseStateMachine(lic)
        # raises LicenseStateMachineError if invalid
        sm.transition(to, reason=reason)

        lic.state = to
        lic.updated_at = datetime.now(tz=timezone.utc)
        await self._repo.session.flush()

        await self._invalidate(lic)

        transition_meta = getattr(lic, "_last_transition", {})
        from_state = transition_meta.get("from", "")
        logger.info(
            "Transition licence [%s] : %s → %s (%s)",
            license_id,
            from_state,
            transition_meta.get("to"),
            transition_meta.get("reason"),
        )

        if self._events:
            await self._events.license_transitioned(
                license_id=license_id,
                tenant_id=str(lic.tenant_id),
                from_state=str(from_state),
                to_state=to.value,
                reason=reason,
            )

        return _to_response(lic)

    async def activate(self, license_id: str, reason: str | None = None) -> LicenseResponse:
        return await self.transition(license_id, LicenseState.ACTIVE, reason=reason or "Activation manuelle")

    async def suspend(self, license_id: str, reason: str | None = None) -> LicenseResponse:
        return await self.transition(license_id, LicenseState.SUSPENDED, reason=reason or "Suspension manuelle")

    async def revoke(self, license_id: str, reason: str | None = None) -> LicenseResponse:
        return await self.transition(license_id, LicenseState.REVOKED, reason=reason or "Révocation manuelle")

    async def renew(
        self,
        license_id: str,
        extend_days: int = 365,
        reason: str | None = None,
    ) -> LicenseResponse:
        """Extend expiry and reactivate if expired."""
        lic = await self._repo.get_by_license_id(license_id)
        if lic is None:
            raise ValueError(f"Licence introuvable : {license_id}")

        sm = LicenseStateMachine(lic)

        now = datetime.now(tz=timezone.utc)
        expires_at = _as_utc(lic.expires_at)
        base = expires_at if (expires_at and expires_at > now) else now
        lic.expires_at = base + timedelta(days=extend_days)

        if sm.can_transition(LicenseState.ACTIVE):
            sm.transition(LicenseState.ACTIVE,
                          reason=reason or f"Renouvellement +{extend_days}j")

        lic.updated_at = now
        await self._repo.session.flush()
        await self._invalidate(lic)

        logger.info("Licence [%s] renouvelée jusqu'au %s",
                    license_id, lic.expires_at)

        if self._events:
            await self._events.license_renewed(
                license_id=license_id,
                tenant_id=str(lic.tenant_id),
                expires_at=lic.expires_at.isoformat() if lic.expires_at else None,
            )

        return _to_response(lic)

    async def change_plan(
        self,
        license_id: str,
        plan_id: str,
        reason: str | None = None,
        activate: bool = True,
    ) -> LicenseResponse:
        """
        Bascule une licence sur un autre plan (upgrade/downgrade).
        Déclenché par xpayproxy après paiement / changement d'abonnement.
        Les features/quotas dérivent du plan (join) — pas besoin de réémettre la clé.
        Si `activate`, force l'état ACTIVE (un changement payé implique l'activation).
        """
        from ..repositories.license import LicensePlanRepository

        lic = await self._repo.get_by_license_id(license_id)
        if lic is None:
            raise ValueError(f"Licence introuvable : {license_id}")

        plan_repo = self._plan_repo or LicensePlanRepository(self._repo.session)
        plan = await plan_repo.get(UUID(str(plan_id)))
        if plan is None:
            raise ValueError(f"Plan introuvable : {plan_id}")

        lic.plan_id = plan.id

        if activate:
            sm = LicenseStateMachine(lic)
            if sm.can_transition(LicenseState.ACTIVE):
                sm.transition(
                    LicenseState.ACTIVE,
                    reason=reason or "Changement de plan (paiement)",
                )

        lic.updated_at = datetime.now(tz=timezone.utc)
        await self._repo.session.flush()
        await self._invalidate(lic)

        logger.info(
            "Licence [%s] changée de plan → %s (%s)", license_id, plan.name, reason
        )

        if self._events:
            await self._events.license_plan_changed(
                license_id=license_id,
                tenant_id=str(lic.tenant_id),
                plan_id=str(plan.id),
                reason=reason,
            )

        return _to_response(lic)

    # ── Auto-expiry sweep ─────────────────────────────────────────────────────

    async def expire_stale(self) -> list[str]:
        """
        Check all ACTIVE/TRIAL licences and expire those past their deadline.
        Meant to be called by a scheduler (e.g. every hour).
        Returns list of expired license IDs.
        """
        from ..models.license import LicenseState

        expired_ids: list[str] = []

        for state in (LicenseState.ACTIVE, LicenseState.TRIAL):
            result = await self._repo.session.execute(
                __import__("sqlalchemy", fromlist=["select"])
                .select(License)
                .where(License.state == state)
                .where(License.expires_at < datetime.now(tz=timezone.utc))
            )
            licenses = result.scalars().all()

            for lic in licenses:
                sm = LicenseStateMachine(lic)
                if sm.can_transition(LicenseState.EXPIRED):
                    sm.transition(LicenseState.EXPIRED,
                                  reason="Expiration automatique (sweep)")
                    lic.updated_at = datetime.now(tz=timezone.utc)
                    await self._invalidate(lic)
                    expired_ids.append(str(lic.id))
                    if self._events:
                        await self._events.license_expired(
                            license_id=str(lic.id),
                            tenant_id=str(lic.tenant_id),
                        )

        if expired_ids:
            await self._repo.session.flush()
            logger.info("Auto-expiry : %d licence(s) expirée(s)",
                        len(expired_ids))

        return expired_ids

    # ── Validation ────────────────────────────────────────────────────────────

    async def validate(self, license_key: str) -> dict:
        """
        Full validation:
            1. JWT signature
            2. Licence exists in DB / cache
            3. State machine auto-expire check
        Returns dict with {valid, state, reason, license_id, tenant_id, expires_at}
        """
        # 1. JWT
        try:
            claims = self._token.verify_license_token(license_key)
        except ValueError as exc:
            return {"valid": False, "reason": str(exc), "state": "invalid_token"}

        license_id = claims.get("sub")
        tenant_id = claims.get("tenant_id")

        # 2. DB lookup (cache-aside)
        lic = await self._load_with_cache(license_id)
        if lic is None:
            return {"valid": False, "reason": "Licence non trouvée", "state": "not_found"}

        if not self._token.verify_token_signature(license_key, lic.license_hash):
            return {
                "valid": False,
                "reason": "Clé de licence révoquée ou obsolète",
                "state": "rotated",
            }

        # 3. Auto-expire
        sm = LicenseStateMachine(lic)
        if sm.auto_expire():
            lic.updated_at = datetime.now(tz=timezone.utc)
            await self._repo.session.flush()
            await self._invalidate(lic)

        is_valid = lic.state == LicenseState.ACTIVE

        # Update last_validation_at
        if is_valid:
            lic.last_validation_at = datetime.now(tz=timezone.utc)
            await self._repo.session.flush()

        return {
            "valid": is_valid,
            "state": lic.state.value,
            "reason": "" if is_valid else f"Licence {lic.state.value}",
            "license_id": str(lic.id),
            "tenant_id": tenant_id,
            "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
        }

    async def get_active_license_for_tenant(self, tenant_id: str) -> dict | None:
        """
        Loads the active license for a tenant, runs auto-expire, and returns a dict
        optimized for middleware consumption.
        """
        # 1. Cache hit
        if self._cache:
            try:
                raw = await self._cache.get(_CACHE_KEY_ACTIVE.format(tenant_id=tenant_id))
                if raw:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    # Reconstruct lightweight object to run auto_expire if needed
                    lic = _dict_to_lic(data)
                    sm = LicenseStateMachine(lic)
                    if sm.auto_expire():
                        # If expired in memory, we need to invalidate and fallback to DB to persist
                        await self._invalidate(lic)
                    else:
                        return data
            except Exception:
                pass

        # 2. DB lookup
        licenses = await self._repo.get_all_by(tenant_id=tenant_id)
        if not licenses:
            return None

        # Sort: ACTIVE first, then most recent
        licenses.sort(
            key=lambda l: (l.state == LicenseState.ACTIVE, l.issued_at),
            reverse=True
        )
        lic = licenses[0]

        # 3. Auto-expire
        sm = LicenseStateMachine(lic)
        if sm.auto_expire():
            lic.updated_at = datetime.now(tz=timezone.utc)
            await self._repo.session.flush()

        # 4. Charge le plan pour exposer modules/features/quotas (entitlement)
        plan = None
        try:
            plan_repo = self._plan_repo or LicensePlanRepository(self._repo.session)
            plan = await plan_repo.get(UUID(str(lic.plan_id)))
        except Exception as exc:
            logger.debug("Chargement plan (entitlement) échoué: %s", exc)

        # 5. Cache set + retour (dict enrichi du plan)
        data = _lic_to_dict(lic, plan)
        if self._cache:
            try:
                await self._cache.set(
                    _CACHE_KEY_ACTIVE.format(tenant_id=tenant_id),
                    json.dumps(data),
                    ttl=_CACHE_TTL,
                )
            except Exception as exc:
                logger.debug("Cache set (active) error: %s", exc)

        return data

    # ── Key rotation ──────────────────────────────────────────────────────────

    async def rotate_key(self, license_id: str) -> LicenseResponse:
        lic = await self._repo.get_by_license_id(license_id)
        if lic is None:
            raise ValueError(f"Licence introuvable : {license_id}")

        expires_at = _as_utc(lic.expires_at)
        remaining_days = (
            max(0, (expires_at - datetime.now(tz=timezone.utc)).days)
            if expires_at
            else 30
        )

        new_key = self._token.create_license_token(
            tenant_id=str(lic.tenant_id),
            license_id=str(lic.id),
            state=lic.state.value,
            expires_in_days=remaining_days,
        )
        new_hash = self._token.hash_token(new_key)

        lic.license_key = new_key
        lic.license_hash = new_hash
        lic.updated_at = datetime.now(tz=timezone.utc)
        await self._repo.session.flush()

        await self._invalidate(lic)
        logger.info("Clé rotée pour la licence [%s]", license_id)

        if self._events:
            await self._events.license_key_rotated(
                license_id=license_id,
                tenant_id=str(lic.tenant_id),
            )

        return _to_response(lic)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_for_tenant(self, tenant_id: str) -> list[LicenseResponse]:
        result = await self._repo.get_all_by(tenant_id=tenant_id)
        return [_to_response(l) for l in (result or [])]

    async def get_by_id(self, license_id: str) -> LicenseResponse | None:
        lic = await self._load_with_cache(license_id)
        return _to_response(lic) if lic else None

    # ── Cache helpers ─────────────────────────────────────────────────────────

    async def _cache_set_active(self, tenant_id: str, lic) -> None:
        if not self._cache:
            return
        try:
            await self._cache.set(
                _CACHE_KEY_ACTIVE.format(tenant_id=tenant_id),
                json.dumps(_lic_to_dict(lic)),
                ttl=_CACHE_TTL,
            )
        except Exception as exc:
            logger.debug("Cache set (active) error: %s", exc)

    async def _cache_set_by_id(self, license_id: str, lic) -> None:
        if not self._cache:
            return
        try:
            await self._cache.set(
                _CACHE_KEY_BY_ID.format(license_id=license_id),
                json.dumps(_lic_to_dict(lic)),
                ttl=_CACHE_TTL,
            )
        except Exception as exc:
            logger.debug("Cache set (by_id) error: %s", exc)

    async def _invalidate(self, lic) -> None:
        if not self._cache:
            return
        for key in (
            _CACHE_KEY_ACTIVE.format(tenant_id=str(lic.tenant_id)),
            _CACHE_KEY_BY_ID.format(license_id=str(lic.id)),
        ):
            try:
                await self._cache.delete(key)
            except Exception:
                pass

    async def _load_with_cache(self, license_id: str):
        if self._cache:
            try:
                raw = await self._cache.get(_CACHE_KEY_BY_ID.format(license_id=license_id))
                if raw:
                    return _dict_to_lic(json.loads(raw) if isinstance(raw, str) else raw)
            except Exception:
                pass
        return await self._repo.get_by_license_id(license_id)


# ── Serialization ─────────────────────────────────────────────────────────────

def _lic_to_dict(lic, plan=None) -> dict:
    _plan = plan or getattr(lic, "plan", None)
    return {
        "id": str(lic.id),
        "tenant_id": str(lic.tenant_id),
        "plan_id": str(lic.plan_id),
        "state": (
            lic.state.value if hasattr(lic.state, "value") else str(lic.state)
        ),
        "license_key": lic.license_key,
        "license_hash": lic.license_hash,
        "issued_at": (
            lic.issued_at.isoformat()
            if getattr(lic, "issued_at", None) else None
        ),
        "expires_at": (
            lic.expires_at.isoformat()
            if getattr(lic, "expires_at", None) else None
        ),
        "last_validation_at": (
            lic.last_validation_at.isoformat()
            if getattr(lic, "last_validation_at", None) else None
        ),
        "updated_at": (
            lic.updated_at.isoformat()
            if getattr(lic, "updated_at", None) else None
        ),
        "features": getattr(_plan, "features", None) or {},
        "quotas": getattr(_plan, "quotas", None) or {},
        "modules": getattr(_plan, "modules", None) or [],
    }


def _dict_to_lic(data: dict):
    """Reconstruit un proxy depuis le cache."""
    class _Proxy:
        pass

    p = _Proxy()
    p.id = data["id"]
    p.tenant_id = data["tenant_id"]
    p.plan_id = data["plan_id"]
    p.state = LicenseState(data["state"])
    p.license_key = data.get("license_key", "")
    p.license_hash = data.get("license_hash", "")

    def _dt(s):
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    p.issued_at = _dt(data.get("issued_at"))
    p.expires_at = _dt(data.get("expires_at"))
    p.last_validation_at = _dt(data.get("last_validation_at"))
    p.updated_at = _dt(data.get("updated_at"))
    p._last_transition = None
    return p


def _to_response(lic) -> LicenseResponse:
    return LicenseResponse(
        id=lic.id,
        tenant_id=str(lic.tenant_id),
        plan_id=lic.plan_id,
        state=lic.state.value if hasattr(
            lic.state, "value") else str(lic.state),
        license_key=lic.license_key,
        license_hash=lic.license_hash,
        issued_at=getattr(lic, "issued_at", None),  # type: ignore
        expires_at=getattr(lic, "expires_at", None),
        last_validation_at=getattr(lic, "last_validation_at", None),
        extra=getattr(lic, "extra", None),  # type: ignore
    )

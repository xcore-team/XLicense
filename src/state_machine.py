"""
State machine for license lifecycle.

Transitions:
    TRIAL      → ACTIVE    (after payment / manual activation)
    TRIAL      → EXPIRED   (TTL expired)
    TRIAL      → REVOKED   (fraud / abuse)
    ACTIVE     → SUSPENDED (non-payment / manual)
    ACTIVE     → EXPIRED   (expires_at < now)
    ACTIVE     → REVOKED   (permanent termination)
    SUSPENDED  → ACTIVE    (payment settled / manual reactivation)
    SUSPENDED  → REVOKED   (permanent after suspension)
    EXPIRED    → ACTIVE    (renewal)
    EXPIRED    → REVOKED   (cleanup)
    REVOKED    → (terminal — no further transition)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .models.license import LicenseState

# ── Transition table ──────────────────────────────────────────────────────────
# { from_state: { to_state: guard | None } }
# guard(license) → bool  — True means transition is allowed

_TRANSITIONS: dict[LicenseState, dict[LicenseState, Callable | None]] = {
    LicenseState.TRIAL: {
        LicenseState.ACTIVE: None,
        LicenseState.EXPIRED: None,
        LicenseState.REVOKED: None,
    },
    LicenseState.ACTIVE: {
        LicenseState.SUSPENDED: None,
        LicenseState.EXPIRED: None,
        LicenseState.REVOKED: None,
    },
    LicenseState.SUSPENDED: {
        LicenseState.ACTIVE: None,
        LicenseState.REVOKED: None,
    },
    LicenseState.EXPIRED: {
        LicenseState.ACTIVE: None,  # renewal
        LicenseState.REVOKED: None,
    },
    LicenseState.REVOKED: {},  # terminal
}

# Human-readable reason defaults per transition
_DEFAULT_REASONS: dict[tuple[LicenseState, LicenseState], str] = {
    (LicenseState.TRIAL, LicenseState.ACTIVE): "Licence activée",
    (LicenseState.TRIAL, LicenseState.EXPIRED): "Période d'essai expirée",
    (LicenseState.TRIAL, LicenseState.REVOKED): "Licence révoquée",
    (LicenseState.ACTIVE, LicenseState.SUSPENDED): "Licence suspendue",
    (LicenseState.ACTIVE, LicenseState.EXPIRED): "Licence expirée",
    (LicenseState.ACTIVE, LicenseState.REVOKED): "Licence révoquée",
    (LicenseState.SUSPENDED, LicenseState.ACTIVE): "Licence réactivée",
    (LicenseState.SUSPENDED, LicenseState.REVOKED): "Licence révoquée définitivement",
    (LicenseState.EXPIRED, LicenseState.ACTIVE): "Licence renouvelée",
    (LicenseState.EXPIRED, LicenseState.REVOKED): "Licence révoquée après expiration",
}


class LicenseStateMachineError(Exception):
    """Raised when a transition is not allowed."""


class LicenseStateMachine:
    """
    Pure state machine — no DB, no side effects.
    Persistence is handled by LicenseService.

    Usage:
        sm = LicenseStateMachine(license)
        sm.transition(LicenseState.SUSPENDED, reason="Impayé")
        # license.state is now SUSPENDED
        # license.state_changed_at is set
    """

    def __init__(self, license) -> None:  # license: License model instance
        self._license = license

    @property
    def state(self) -> LicenseState:
        return self._license.state

    def can_transition(self, to: LicenseState) -> bool:
        allowed = _TRANSITIONS.get(self.state, {})
        return to in allowed

    def allowed_transitions(self) -> list[LicenseState]:
        return list(_TRANSITIONS.get(self.state, {}).keys())

    def transition(self, to: LicenseState, reason: str | None = None) -> None:
        """
        Apply transition in-place on the license object.
        Raises LicenseStateMachineError if not allowed.
        """
        if not self.can_transition(to):
            allowed = [s.value for s in self.allowed_transitions()]
            raise LicenseStateMachineError(
                f"Transition {self.state.value!r} → {to.value!r} interdite. "
                f"Transitions autorisées depuis '{self.state.value}' : {allowed}"
            )

        # Run guard if defined
        guard = _TRANSITIONS[self.state].get(to)
        if guard and not guard(self._license):
            raise LicenseStateMachineError(
                f"Transition {self.state.value!r} → {to.value!r} refusée par la garde."
            )

        from_ = self.state
        self._license.state = to
        self._license.updated_at = datetime.now(tz=timezone.utc)

        # Attach metadata to the license object for the caller to persist
        self._license._last_transition = {
            "from": from_.value,
            "to": to.value,
            "reason": reason or _DEFAULT_REASONS.get((from_, to), ""),
            "at": self._license.updated_at.isoformat(),
        }

    def auto_expire(self) -> bool:
        """
        Automatically transition to EXPIRED if expires_at has passed.
        Returns True if the transition was applied.
        """
        if self._license.expires_at is None:
            return False
        now = datetime.now(tz=timezone.utc)
        expires = self._license.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now and self.can_transition(LicenseState.EXPIRED):
            self.transition(LicenseState.EXPIRED, reason="Expiration automatique")
            return True
        return False

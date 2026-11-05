"""Ce qu'un événement du prestataire de paiement change pour un module d'une organisation.

Séparé de la route exprès : décider est une RÈGLE (pure, testable sans Stripe ni base), appliquer
est une écriture. La route vérifie la signature, appelle `decide()`, puis applique.

Le vocabulaire des événements est celui, normalisé, de `extensions/xpay` (`_EVENT_MAP`) : il ne
dépend pas de Stripe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

# Ce qu'il faut faire du module visé.
#   keep     : l'abonnement court — pas d'échéance
#   until    : il s'arrête à une date (résiliation en fin de période payée)
#   end_now  : il s'arrête tout de suite (abonnement supprimé ou suspendu)
#   ignore   : cet événement ne change rien à l'entitlement
Action = Literal["keep", "until", "end_now", "ignore"]


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    tenant_id: Optional[str] = None
    module_code: Optional[str] = None
    subscription_id: Optional[str] = None
    until: Optional[datetime] = None
    reason: str = ""

    @property
    def targets_a_module(self) -> bool:
        """Sait-on QUEL module est visé ? (métadonnées, ou abonnement retrouvé en base)"""
        return bool(self.tenant_id and self.module_code) or bool(self.subscription_id)


def _object(event: Any) -> dict:
    """L'objet du prestataire, pour ce que l'événement normalisé ne reprend pas (métadonnées,
    `cancel_at_period_end`, `current_period_end`). Jamais une erreur si la forme change."""
    raw = getattr(event, "raw", None) or {}
    try:
        return raw["data"]["object"] or {}
    except (KeyError, TypeError):
        return {}


def decide(event: Any) -> Decision:
    """Traduit un événement normalisé en intention, sans rien écrire.

    Choix assumé sur l'échec de paiement : on ne coupe PAS. Le prestataire réessaie plusieurs
    jours ; couper au premier échec priverait une organisation qui paiera demain. C'est la
    suppression de l'abonnement (`subscription.cancelled`) qui met fin à l'accès.
    """
    kind = getattr(event, "event_type", "") or ""
    data = getattr(event, "data", None) or {}
    obj = _object(event)
    meta = obj.get("metadata") or {}

    base = {
        "tenant_id": (meta.get("tenant_id") or "").strip() or None,
        "module_code": (meta.get("module_code") or "").strip() or None,
        # Pour une résiliation, l'objet EST l'abonnement ; pour une facture, il le cite.
        "subscription_id": data.get("subscription_id") or None,
    }

    if kind in ("invoice.paid", "payment.succeeded", "subscription.created", "subscription.resumed"):
        return Decision(action="keep", reason=kind, **base)

    if kind in ("subscription.cancelled", "subscription.paused"):
        return Decision(action="end_now", reason=kind, **base)

    if kind == "subscription.updated":
        # Résiliation programmée : le module reste jusqu'au bout de la période RÉGLÉE.
        if obj.get("cancel_at_period_end"):
            end = obj.get("current_period_end")
            when = (
                datetime.fromtimestamp(int(end), tz=timezone.utc)
                if isinstance(end, (int, float))
                else None
            )
            if when is None:
                # Résiliation annoncée sans date : on ne coupe pas sur une supposition, on attend
                # la suppression de l'abonnement.
                return Decision(action="ignore", reason=f"{kind}:sans_echeance", **base)
            return Decision(action="until", until=when, reason=kind, **base)
        status = (data.get("status") or "").lower()
        if status in ("canceled", "cancelled", "unpaid", "incomplete_expired"):
            return Decision(action="end_now", reason=f"{kind}:{status}", **base)
        return Decision(action="keep", reason=kind, **base)

    # payment.failed, invoice.upcoming, payment.expired, et tout ce qu'on ne connaît pas :
    # l'entitlement ne bouge pas.
    return Decision(action="ignore", reason=kind or "inconnu", **base)

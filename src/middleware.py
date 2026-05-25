from __future__ import annotations

"""
XLicense middleware — vérifie la licence du tenant à chaque requête.

Fonctionnement :
    1. Extrait le tenant_id depuis le JWT (Authorization: Bearer <token>)
       ou depuis le header X-Tenant-Id.
    2. Charge la licence depuis le cache (Redis) ou la DB.
    3. Applique l'auto-expiration via la state machine.
    4. Injecte dans request.state :
         request.state.license         → objet License (ou None)
         request.state.license_valid   → bool
         request.state.license_state   → str (ex: "active")
    5. Injecte dans les headers de réponse :
         X-License-State  : active | expired | suspended | revoked | unknown
         X-License-Valid  : true | false
         X-License-Id     : <uuid> (si trouvé)
         X-License-Exp    : <ISO date> (si définie)
    6. Retourne 402 Payment Required si la licence n'est pas active
       ET que la route est dans PROTECTED_PREFIXES.

Configurer les routes non protégées via EXCLUDED_PREFIXES.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger("xlicense.middleware")

# Routes par défaut (si non fournies dans le manifeste)
DEFAULT_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/docs",
    "/redoc",
    "/openapi",
    "/health",
    "/xlicense/verify",
    "/app/auth/login",
    "/app/auth/register",
    "/app/auth/refresh",
    "/app/auth/logout",
    "/app/auth/select-tenant",
    "/app/auth/oauth",
    "/app/auth/me",
    "/app/auth/tenants",
    "/app/auth/invites",
    "/app/auth/password/forgot",
    "/app/auth/password/reset",
    "/app/xlicense/me",
    "/app/xlicense/plans",
    "/app/xlicense/licenses",
    "/app/xlicense/verify",
)


class LicenseMiddleware(BaseHTTPMiddleware):
    """
    Middleware de vérification de licence par tenant utilisant le bus d'événements Xcore.
    Récupère sa configuration (enforce, prefixes) dynamiquement via l'événement xlicense.config.

    Args:
        app:                ASGI app
        emit:               Fonction d'émission d'événements (xcore.events.emit)
        verify_event:       Nom de l'événement de vérification (default: "xlicense.verify")
        config_event:       Nom de l'événement de configuration (default: "xlicense.config")
    """

    def __init__(
        self,
        app: ASGIApp,
        emit: Callable[[str, dict[str, Any] | None], Any] | None = None,
        verify_event: str = "xlicense.verify",
        config_event: str = "xlicense.config",
    ) -> None:
        super().__init__(app)
        self._emit = emit
        self._verify_event = verify_event
        self._config_event = config_event
        self._config_loaded = False

        # Defaults
        self._enforce = True
        self._excluded = DEFAULT_EXCLUDED_PREFIXES
        self._protected = None

    async def _load_config(self) -> None:
        """Charge la configuration depuis le plugin license."""
        if self._config_loaded or not self._emit:
            return

        try:
            results = await self._emit(self._config_event, {})
            print(results)
            if results and isinstance(results[0], dict):
                plugin_config = results[0]
                self._enforce = plugin_config.get("enforce", True)

                excluded = plugin_config.get("excluded_prefixes")
                if excluded is not None:
                    self._excluded = tuple(excluded)

                protected = plugin_config.get("protected_prefixes")
                if protected:
                    self._protected = tuple(protected)

                self._config_loaded = True
        except Exception as exc:
            logger.error("Failed to load LicenseMiddleware config: %s", exc)

    # ── Middleware entry point ────────────────────────────────────────────────

    async def dispatch(self, request: Request, call_next) -> Response:
        # Assurer que la configuration est chargée
        if not self._config_loaded:
            await self._load_config()

        path = request.url.path

        # Skip excluded routes
        if self._is_excluded(path):
            return await call_next(request)

        tenant_id = self._extract_tenant_id(request)

        license_data = None
        is_valid = False
        license_state = "unknown"
        license_id = None
        license_exp = None

        if tenant_id and self._emit:
            # Appel au plugin license via le bus d'événements
            results = await self._emit(self._verify_event, {"tenant_id": tenant_id})
            license_data = results[0] if results else None

            if license_data:
                license_state = license_data.get("state", "unknown")
                license_id = license_data.get("id")
                is_valid = license_state == "active"
                license_exp = license_data.get("expires_at")

        # Inject into request.state for downstream use
        request.state.license = license_data
        request.state.license_valid = is_valid
        request.state.license_state = license_state
        request.state.license_tenant_id = tenant_id

        logger.debug("license_state: %s, is_valid: %s", license_state, is_valid)
        # Enforce check
        if self._enforce and not is_valid:
            if self._requires_protection(path):
                return self._reject(license_state, tenant_id)

        response = await call_next(request)

        # Inject response headers
        response.headers["X-License-State"] = license_state
        response.headers["X-License-Valid"] = "true" if is_valid else "false"
        if license_id:
            response.headers["X-License-Id"] = license_id
        if license_exp:
            response.headers["X-License-Exp"] = license_exp

        logger.debug(f"Response: {response.status_code} {response.headers}")

        return response

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_excluded(self, path: str) -> bool:
        return any(path.startswith(p) for p in self._excluded)

    def _requires_protection(self, path: str) -> bool:
        if self._protected is None:
            return True
        return any(path.startswith(p) for p in self._protected)

    def _extract_tenant_id(self, request: Request) -> str | None:
        """
        Cherche le tenant_id dans :
            1. request.state (si XAuthBackend a déjà décodé le token)
            2. Header X-Tenant-Id (explicite)
            3. JWT Authorization — decode WITHOUT vérification pour extraire tenant_id
        """
        if hasattr(request.state, "user") and request.state.user:
            return request.state.user.get("tenant_id") or request.state.user.get(
                "user", {}
            ).get("tenant_id")

        tenant_id = request.headers.get("X-Tenant-Id")
        if tenant_id:
            return tenant_id

        auth = (
            request.headers.get("Authorization")
            or request.headers.get("authorization")
            or ""
        )
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if token:
                try:
                    from jose import jwt as jose_jwt

                    claims = jose_jwt.get_unverified_claims(token)
                    return claims.get("tenant_id")
                except Exception:
                    pass
        return None

    @staticmethod
    def _reject(license_state: str, tenant_id: str | None) -> JSONResponse:
        messages = {
            "expired": "La licence de ce tenant a expiré. Veuillez renouveler votre abonnement.",
            "suspended": "La licence de ce tenant est suspendue. Veuillez contacter le support.",
            "revoked": "La licence de ce tenant a été révoquée.",
            "unknown": "Aucune licence active trouvée pour ce tenant.",
            "trial": "La période d'essai est terminée. Veuillez activer votre licence.",
        }
        return JSONResponse(
            status_code=402,
            content={
                "error": "license_required",
                "state": license_state,
                "tenant_id": tenant_id,
                "message": messages.get(license_state, "Licence invalide."),
            },
            headers={
                "X-License-State": license_state,
                "X-License-Valid": "false",
            },
        )

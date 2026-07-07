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

from __future__ import annotations

import logging
from typing import Any, Callable
from xcore.sdk import get_logger
from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = get_logger("xlicense.middleware")

# Routes par défaut (si non fournies dans le manifeste)
DEFAULT_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/docs",
    "/redoc",
    "/openapi",
    "/health",
    "/xlicense/verify",
    "/app/auth/login",
    "/app/auth/register",
    "/app/auth/setup",
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
    "/app/xform/*"
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
        emit: Any = None,
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
        # Module-gate (entitlement par plan) — OPT-IN pour ne rien casser tant que
        # les modules des plans ne sont pas configurés. Activer via config
        # enforce_modules: true. core_modules = toujours accessibles (auth/licence).
        self._enforce_modules = False
        self._core_modules: tuple[str, ...] = ("auth", "xlicense")
        self._plugin_prefix = "/app"

    async def _emit_event(self, name: str, data: dict[str, Any]) -> list[Any]:
        """
        Émet un événement et retourne la liste des résultats des handlers.

        `emit` est injecté via integration.yaml. On accepte :
          - un EventBus (type: events) → on appelle son .emit(name, data)
          - un callable direct emit(name, data)
        """
        em = self._emit
        if em is None:
            return []

        # 1. EventBus injecté (type: events) → .emit(name, data)
        if hasattr(em, "emit"):
            return (await em.emit(name, data)) or []

        # 2. Callable direct emit(name, data)
        if callable(em):
            try:
                return (await em(name, data)) or []
            except TypeError:
                # 3. Résolveur 0-arg (type: internal) → renvoie le service/bus
                resolved = em()
                if resolved is not None and hasattr(resolved, "emit"):
                    return (await resolved.emit(name, data)) or []
        return []

    async def _load_config(self) -> None:
        """Charge la configuration depuis le plugin license."""
        if self._config_loaded or not self._emit:
            return

        try:
            results = await self._emit_event(self._config_event, {})
            if results and isinstance(results[0], dict):
                plugin_config = results[0]
                self._enforce = plugin_config.get("enforce", True)

                excluded = plugin_config.get("excluded_prefixes")
                if excluded is not None:
                    self._excluded = tuple(excluded)

                protected = plugin_config.get("protected_prefixes")
                if protected:
                    self._protected = tuple(protected)

                self._enforce_modules = plugin_config.get(
                    "enforce_modules", self._enforce_modules
                )
                core = plugin_config.get("core_modules")
                if core is not None:
                    self._core_modules = tuple(core)
                plugin_prefix = plugin_config.get("plugin_prefix")
                if plugin_prefix:
                    self._plugin_prefix = plugin_prefix

                self._config_loaded = True
        except Exception as exc:
            logger.error("Failed to load LicenseMiddleware config: %s", exc)

            logger.exception(exc)

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
            results = await self._emit_event(
                self._verify_event, {"tenant_id": tenant_id}
            )
            license_data = results[0] if results else None

            if license_data:
                license_state = license_data.get("state", "unknown")
                license_id = license_data.get("id")
                is_valid = license_state in ("active", "trial")
                license_exp = license_data.get("expires_at")

        # Inject into request.state for downstream use
        request.state.license = license_data
        request.state.license_valid = is_valid
        request.state.license_state = license_state
        request.state.license_tenant_id = tenant_id

        logger.debug("license_state: %s, is_valid: %s", license_state, is_valid)
        # Enforce check — la licence est PAR TENANT. Sans tenant résolu (onboarding,
        # création de tenant, requêtes non scopées), il n'y a pas de licence à vérifier :
        # on laisse passer (les routes protégées restent gardées par l'auth/tenant).
        if tenant_id and self._enforce and not is_valid:
            if self._requires_protection(path):
                return self._reject(license_state, tenant_id)

        # Module-gate : la licence est valide mais le module appelé est-il inclus
        # dans le plan du tenant ? (entitlement). Opt-in via enforce_modules.
        if (
            tenant_id
            and self._enforce_modules
            and is_valid
            and license_data is not None
            and not self._is_excluded(path)
        ):
            module = self._module_for_path(path)
            if module and not self._module_allowed(
                module, license_data.get("modules", [])
            ):
                return self._reject_module(module, tenant_id)

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

    @staticmethod
    def _normalize_prefix(p: str) -> str:
        """Supprime les globs de fin ('*', '/*') pour que startswith fonctionne."""
        return p.rstrip("*").rstrip("/") if p.endswith("*") else p

    def _is_excluded(self, path: str) -> bool:
        return any(path.startswith(self._normalize_prefix(p)) for p in self._excluded)

    def _requires_protection(self, path: str) -> bool:
        if self._protected is None:
            return True
        return any(path.startswith(p) for p in self._protected)

    def _module_for_path(self, path: str) -> str | None:
        """Déduit le module (plugin) ciblé = 1er segment après le plugin_prefix.

        /app/xwms/stock → "xwms" ; /app/auth/me → "auth".
        """
        p = path
        if self._plugin_prefix and p.startswith(self._plugin_prefix):
            p = p[len(self._plugin_prefix):]
        p = p.lstrip("/")
        if not p:
            return None
        return p.split("/", 1)[0]

    def _module_allowed(self, module: str, modules: list[str]) -> bool:
        """Le module est-il dans l'entitlement du plan ?

        core_modules (auth/licence) toujours autorisés ; ["*"] = tous.
        """
        if module in self._core_modules:
            return True
        if modules and "*" in modules:
            return True
        return module in (modules or [])

    @staticmethod
    def _reject_module(module: str, tenant_id: str | None) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": "module_not_in_plan",
                "module": module,
                "tenant_id": tenant_id,
                "message": (
                    f"Le module « {module} » n'est pas inclus dans le plan de ce "
                    "tenant. Mettez à niveau votre abonnement pour y accéder."
                ),
            },
            headers={"X-License-Module": module, "X-License-Module-Allowed": "false"},
        )

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
                    raise HTTPException(status_code=401, detail="Invalid token")
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

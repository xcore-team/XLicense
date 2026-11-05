from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


import jwt
from jwt import PyJWTError as JWTError

ALGORITHM = "RS256"

# Racine de l'application (dossier de integration.yaml) : app/Xlicense/src/services/jwt.py → 4 niveaux.
APP_ROOT = Path(__file__).resolve().parents[4]

_GENERER = (
    "Pour générer une paire de clés : "
    "openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out conf/private.pem && "
    "openssl rsa -pubout -in conf/private.pem -out conf/public.pem "
    "(la clé publique doit correspondre à la clé privée)."
)


class LicenseKeyError(RuntimeError):
    """Clé de licence absente ou illisible : le plugin démarre en état dégradé."""


def resolve_key_paths(env: Any) -> tuple[Path, Path]:
    """Chemins (privé, public) des clés de licence.

    Priorité : variables XLICENSE_PRIVATE_KEY_PATH / XLICENSE_PUBLIC_KEY_PATH, puis
    JWT_PRIVATE_KEY_PATH / JWT_PUBLIC_KEY_PATH du plugin. Un chemin relatif est résolu par rapport
    à la RACINE de l'application et non au répertoire courant du processus.
    """
    def pick(*names: str) -> str:
        for name in names:
            val = os.environ.get(name) or (env.get(name) if env else None)
            if val:
                return str(val)
        raise LicenseKeyError(f"Variable {' ou '.join(names)} non définie. {_GENERER}")

    def absolu(raw: str) -> Path:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (APP_ROOT / p).resolve()

    return (
        absolu(pick("XLICENSE_PRIVATE_KEY_PATH", "JWT_PRIVATE_KEY_PATH")),
        absolu(pick("XLICENSE_PUBLIC_KEY_PATH", "JWT_PUBLIC_KEY_PATH")),
    )


def _lire(path: str | Path, variable: str, role: str) -> str:
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise LicenseKeyError(
            f"Clé {role} de licence illisible : {path} ({exc.strerror or exc}). "
            f"Définir {variable} (chemin absolu) ou placer le fichier à cet emplacement. {_GENERER} "
            "Les routes de licence répondent 503 tant que la clé est absente."
        ) from exc


class LicenseTokenService:

    def __init__(self,
                 private_key_path: str | Path,
                 public_key_path: str | Path,
                 ) -> None:

        self._private_key = _lire(private_key_path, "XLICENSE_PRIVATE_KEY_PATH", "privée")
        self._public_key = _lire(public_key_path, "XLICENSE_PUBLIC_KEY_PATH", "publique")

    def create_license_token(self, license_id: str, tenant_id: str, state: str = "TRIAL", expires_in_days: int = 30, extra: dict = None) -> str:
        """Génère un token JWT pour une licence donnée."""
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": license_id,
            "jti": str(uuid4()),
            "iat": now,
            # Gestion automatique de l'expiration
            "exp": now + timedelta(days=expires_in_days),
            "tenant_id": tenant_id,
            "state": state,  # L'état actuel de la machine
            "type": "license",
        }

        if extra:
            payload.update(extra)
        return jwt.encode(payload, self._private_key, algorithm=ALGORITHM)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def verify_token_signature(self, token: str, hash: str) -> bool:
        return hashlib.sha256(token.encode()).hexdigest() == hash

    def refresh_license_token(self, token: str, extra: dict[str, Any] | None = None) -> str:
        try:
            payload = jwt.decode(token, self._public_key,
                                 algorithms=[ALGORITHM])
            if payload.get("type") != "license":
                raise JWTError("Invalid token type")
            license_id = payload["sub"]
            tenant_id = payload["tenant_id"]
            return self.create_license_token(
                license_id, tenant_id, state=payload.get("state", "TRIAL"), extra=extra
            )
        except JWTError as e:
            raise JWTError(f"Token refresh failed: {str(e)}") from e

    def verify_license_token(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(token, self._public_key,
                                 algorithms=[ALGORITHM])
        except JWTError as exc:
            raise ValueError(f"Token invalide : {exc}") from exc
        if payload.get("type") != "license":
            raise ValueError("Type de token invalide")
        return payload


class DegradedTokenService:
    """Remplace LicenseTokenService quand la clé est absente : toute utilisation lève LicenseKeyError."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __getattr__(self, name: str):
        def _indisponible(*_a: Any, **_k: Any):
            raise LicenseKeyError(self.reason)
        return _indisponible

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jose import JWTError, jwt

ALGORITHM = "RS256"


class LicenseTokenService:
    def __init__(
        self,
        private_key_path: str | Path,
        public_key_path: str | Path,
    ) -> None:
        self._private_key = Path(private_key_path).read_text()
        self._public_key = Path(public_key_path).read_text()

    def create_license_token(
        self,
        license_id: str,
        tenant_id: str,
        state: str = "trial",
        expires_in_days: int = 30,
        extra: dict | None = None,
    ) -> str:
        """Génère un token JWT pour une licence donnée."""
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": license_id,
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(days=expires_in_days),
            "tenant_id": tenant_id,
            "state": state,
            "type": "license",
        }

        if extra:
            payload.update(extra)

        return jwt.encode(payload, self._private_key, algorithm=ALGORITHM)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def verify_token_signature(self, token: str, hash: str) -> bool:
        return hashlib.sha256(token.encode()).hexdigest() == hash

    def refresh_license_token(
        self, token: str, extra: dict[str, Any] | None = None
    ) -> str:
        """Renouvelle un token JWT en conservant l'identité et l'état existants."""
        try:
            payload = jwt.decode(token, self._public_key, algorithms=[ALGORITHM])
            if payload.get("type") != "license":
                raise JWTError("Invalid token type")
            # FIX: transmettre tous les paramètres requis par create_license_token
            return self.create_license_token(
                license_id=payload["sub"],
                tenant_id=payload["tenant_id"],
                state=payload.get("state", "trial"),
                expires_in_days=30,
                extra=extra,
            )
        except JWTError as e:
            raise JWTError(f"Token refresh failed: {str(e)}") from e

    def verify_license_token(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(token, self._public_key, algorithms=[ALGORITHM])
        except JWTError as exc:
            raise ValueError(f"Token invalide : {exc}") from exc
        if payload.get("type") != "license":
            raise ValueError("Type de token invalide")
        return payload

"""Mise à niveau défensive du schéma xlicense (idempotente, sans perte de données).

``Base.metadata.create_all`` ne modifie pas une table existante. ``licenses.license_key``
était créée en VARCHAR(512) : trop court pour un jeton RS256, ce que PostgreSQL refuse
(SQLite ignore la longueur). Les bases PostgreSQL existantes sont élargies ici ;
élargir un VARCHAR ne réécrit pas la table et ne touche à aucune donnée.
"""
from __future__ import annotations

from sqlalchemy import text

from .models.license import LICENSE_KEY_LENGTH

_LENGTH_SQL = text(
    "SELECT character_maximum_length FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'licenses' "
    "AND column_name = 'license_key'"
)


def ensure_schema_sync(conn) -> list[str]:
    """À appeler via ``await conn.run_sync(ensure_schema_sync)``. Retourne les changements faits."""
    if conn.dialect.name != "postgresql":
        return []
    current = conn.execute(_LENGTH_SQL).scalar()
    if current is None or current >= LICENSE_KEY_LENGTH:
        return []
    conn.execute(
        text(f'ALTER TABLE licenses ALTER COLUMN license_key TYPE VARCHAR({LICENSE_KEY_LENGTH})')
    )
    return [f"licenses.license_key VARCHAR({current}) -> VARCHAR({LICENSE_KEY_LENGTH})"]

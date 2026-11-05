"""élargit licenses.license_key à 2048 caractères

Le jeton de licence est un JWT RS256 (~620 caractères avec une clé de 2048 bits) : PostgreSQL
refusait l'insertion dans un VARCHAR(512). Élargir un VARCHAR ne perd aucune donnée.

Revision ID: 0004_license_key_length
Revises: 0003_license_last_rotated_at
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_license_key_length"
down_revision: Union[str, Sequence[str], None] = "0003_license_last_rotated_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("licenses") as batch:
        batch.alter_column(
            "license_key", existing_type=sa.String(512), type_=sa.String(2048), existing_nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("licenses") as batch:
        batch.alter_column(
            "license_key", existing_type=sa.String(2048), type_=sa.String(512), existing_nullable=False
        )

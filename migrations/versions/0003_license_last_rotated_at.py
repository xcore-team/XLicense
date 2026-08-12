"""add last_rotated_at to licenses

Revision ID: 0003_license_last_rotated_at
Revises: 0002_plan_modules
Create Date: 2026-07-28

Le cooldown de rotation de clé (`POST /xlicense/licenses/{id}/rotate-key`)
mesurait `last_validation_at` (dernière vérification `/verify`) au lieu de la
dernière rotation réelle — cette colonne n'existait pas encore. Audit XLicense
Constat 5.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembics import op

revision: str = "0003_license_last_rotated_at"
down_revision: Union[str, Sequence[str], None] = "0002_plan_modules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "licenses",
        sa.Column(
            "last_rotated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("licenses") as batch:
        batch.drop_column("last_rotated_at")

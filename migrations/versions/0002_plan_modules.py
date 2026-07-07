"""add modules (entitlement) to license_plans

Revision ID: 0002_plan_modules
Revises: 0001_initial
Create Date: 2026-07-02

Phase 2 de l'architecture distribuée : les modules (plugins) inclus dans un plan
deviennent l'ENTITLEMENT du tenant. L'organisation ne stocke plus active_modules.
["*"] = tous les modules.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_plan_modules"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "license_plans",
        sa.Column(
            "modules",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("license_plans") as batch:
        batch.drop_column("modules")

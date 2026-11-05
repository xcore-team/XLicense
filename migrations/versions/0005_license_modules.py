"""catalogue des modules vendables (license_modules)

La couche commerciale d'un SaaS dont la marketplace est DANS l'application : on facture les
plugins installés dans cette instance xcore, à la manière d'Odoo. Quels modules existent reste
l'affaire du registre local (`ctx.registry`), d'où ce catalogue se synchronise ; cette table ne
porte que le prix et la mise en rayon, posés par l'administrateur de la plateforme.

`code` est le nom du plugin = le 1er segment de route après le préfixe, la même clé que
`license_plans.modules` et que le gate par module du middleware.

Revision ID: 0005_license_modules
Revises: 0004_license_key_length
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_license_modules"
down_revision: Union[str, Sequence[str], None] = "0004_license_key_length"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "license_modules",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column("free", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column(
            "period",
            sa.Enum("MONTH", "YEAR", "ONCE", name="billingperiod"),
            nullable=False,
            server_default="MONTH",
        ),
        sa.Column("stripe_price_id", sa.String(255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("license_modules")
    # L'enum nommé ne disparaît pas avec la table sous PostgreSQL.
    sa.Enum(name="billingperiod").drop(op.get_bind(), checkfirst=True)

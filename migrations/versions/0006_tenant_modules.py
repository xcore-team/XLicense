"""entitlement par organisation (license_tenant_modules)

Les modules qu'une organisation a PRIS. Remplace `license_plans.modules` comme source de « ce que
cette organisation a le droit d'utiliser » ; le plan redevient un préréglage commercial. Pendant
l'essai, la question ne se pose pas : le plan d'essai ouvre `["*"]`.

Clé primaire composée (tenant_id, module_code) : la base refuse d'elle-même une double activation.

Revision ID: 0006_tenant_modules
Revises: 0005_license_modules
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_tenant_modules"
down_revision: Union[str, Sequence[str], None] = "0005_license_modules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "license_tenant_modules",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column(
            "module_code",
            sa.String(64),
            sa.ForeignKey("license_modules.code"),
            primary_key=True,
        ),
        sa.Column(
            "source",
            sa.Enum("FREE", "PAID", "GRANTED", name="modulesource"),
            nullable=False,
            server_default="FREE",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_item_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Le gate lit l'entitlement d'UNE organisation à chaque requête : l'index sert cette lecture.
    op.create_index(
        "ix_license_tenant_modules_tenant", "license_tenant_modules", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_license_tenant_modules_tenant", table_name="license_tenant_modules")
    op.drop_table("license_tenant_modules")
    sa.Enum(name="modulesource").drop(op.get_bind(), checkfirst=True)

"""initial

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembics import op

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "license_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column(
            "type",
            sa.Enum("starter", "pro", "enterprise", "lifetime", name="licensetype"),
            nullable=False,
        ),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("max_users", sa.Integer, nullable=False, server_default="1"),
        sa.Column("max_machines", sa.Integer, nullable=False, server_default="1"),
        sa.Column("features", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("quotas", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("stripe_price_id", sa.String(255), nullable=True),
        sa.Column("stripe_product_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "licenses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, index=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("license_plans.id"), nullable=False),
        sa.Column(
            "state",
            sa.Enum("active", "expired", "suspended", "revoked", "trial", name="licensestate"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("license_key", sa.String(512), nullable=False),
        sa.Column("license_hash", sa.String(512), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("license_key"),
    )
    op.create_index("ix_licenses_license_key", "licenses", ["license_key"], unique=True)


def downgrade() -> None:
    op.drop_table("licenses")
    op.drop_table("license_plans")
    op.execute("DROP TYPE IF EXISTS licensestate")
    op.execute("DROP TYPE IF EXISTS licensetype")

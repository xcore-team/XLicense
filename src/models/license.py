import enum
from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (Mapped, declarative_base, mapped_column,
                            relationship)

Base = declarative_base()


class LicenseState(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    TRIAL = "trial"

    @property
    def is_usable(self) -> bool:
        """Un tenant dans cet état peut utiliser la plateforme (pas de 402).

        Source de vérité unique pour « licence valide » — utilisée à la fois
        par le middleware (gate 402) et par `LicenseService.validate()` (route
        publique /verify), qui divergeaient auparavant (ACTIVE+TRIAL contre
        ACTIVE seul).
        """
        return self in (LicenseState.ACTIVE, LicenseState.TRIAL)


class LicenseType(str, enum.Enum):
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    LIFETIME = "lifetime"


class LicensePlan(Base):
    __tablename__ = "license_plans"

    id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(1024), nullable=True)
    type: Mapped[LicenseType] = mapped_column(
        SqlEnum(LicenseType), nullable=False
    )
    price: Mapped[float] = mapped_column(nullable=False)
    max_users: Mapped[int] = mapped_column(nullable=False, default=1)
    max_machines: Mapped[int] = mapped_column(nullable=False, default=1)
    # {"api_access": true, "custom_domain": false, ...}
    features: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # {"max_requests_per_day": 1000, "storage_gb": 5, ...}
    quotas: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Modules (plugins) inclus dans le plan = ENTITLEMENT du tenant.
    # Liste de noms de segment de route (ex: ["xwms", "xcompany", "tasks"]).
    # ["*"] = tous les modules. Source de vérité de « quels modules l'organisation
    # a le droit d'utiliser » — l'org ne stocke plus cette liste.
    modules: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Mapping vers le catalogue Stripe — renseigné par seed (env) ou PATCH /plans/{id}.
    # price_id  → tarif d'abonnement récurrent Stripe (Basic/Pro/Enterprise).
    # product_id → produit du catalogue local xpay pour un achat one-time (Lifetime).
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    licenses = relationship("License", back_populates="plan")


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    plan_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("license_plans.id"),
        nullable=False,
    )
    state: Mapped[LicenseState] = mapped_column(
        SqlEnum(LicenseState), nullable=False, default=LicenseState.ACTIVE
    )
    license_key: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True, index=True
    )
    license_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_validation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    plan = relationship("LicensePlan", back_populates="licenses")

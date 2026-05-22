from datetime import datetime
from uuid import uuid4
import enum

from sqlalchemy import (
    String,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    func,
)

from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    declarative_base,
    relationship,
)

from sqlalchemy.dialects.postgresql import UUID


Base = declarative_base()


# =========================================================
# ENUMS
# =========================================================

class LicenseState(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    TRIAL = "trial"


class LicenseType(str, enum.Enum):
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    LIFETIME = "lifetime"


# =========================================================
# LICENSE PLANS
# =========================================================

class LicensePlan(Base):
    __tablename__ = "license_plans"
    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True),primary_key=True,default=uuid4)
    name: Mapped[str] = mapped_column(String(255),nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(1024),nullable=True)
    type: Mapped[LicenseType] = mapped_column(SqlEnum(LicenseType),nullable=False)
    price: Mapped[float] = mapped_column(nullable=False)
    max_users: Mapped[int] = mapped_column(nullable=False,default=1)
    max_machines: Mapped[int] = mapped_column(nullable=False,default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now(),nullable=False)

    licenses = relationship("License",back_populates="plan")


# =========================================================
# LICENSES
# =========================================================

class License(Base):
    __tablename__ = "licenses"
    # TODO: Ajouter un champ "machine_id" pour lier une licence à une machine spécifique (optionnel)
    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True),primary_key=True,default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(255),nullable=False,index=True)
    plan_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True),ForeignKey("license_plans.id"),nullable=False)
    state: Mapped[LicenseState] = mapped_column(SqlEnum(LicenseState),nullable=False,default=LicenseState.ACTIVE)
    
    license_key: Mapped[str] = mapped_column(String(512),nullable=False,unique=True,index=True)
    license_hash: Mapped[str] = mapped_column(String(512),nullable=False)
    
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=func.now(),nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)

    last_validation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now(),nullable=False)
    plan = relationship("LicensePlan",back_populates="licenses")

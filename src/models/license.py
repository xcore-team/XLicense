import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (Mapped, declarative_base, mapped_column,
                            relationship)

Base = declarative_base()

LICENSE_KEY_LENGTH = 2048


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


class BillingPeriod(str, enum.Enum):
    """Périodicité d'un module payant. `ONCE` = achat définitif (pas d'abonnement)."""

    MONTH = "month"
    YEAR = "year"
    ONCE = "once"


class LicenseModule(Base):
    """Un module VENDABLE du catalogue : ce que l'administrateur de la plateforme met en rayon.

    Le produit est un SaaS et sa marketplace est DANS l'application : on facture les plugins
    installés dans CETTE instance xcore, à la manière d'Odoo. Le catalogue ne dit donc pas quels
    modules existent — ça, c'est le registre local de l'instance (`ctx.registry.all_plugins()`,
    d'où le catalogue se synchronise) — il dit combien ils coûtent : gratuit ou payant, à quel
    prix, sous quelle périodicité. C'est la couche commerciale, et elle seule.

    `code` est le nom du plugin = le premier segment de route après le préfixe (`/app/<code>/…`),
    la même clé que `LicensePlan.modules` et que le gate par module du middleware, et la même que
    le `source_plugin` des permissions d'xauth (c'est par là que les modules servis par le moteur
    sont gardés, leurs routes étant toutes sous `/app/xcore-doctype/`).
    """

    __tablename__ = "license_modules"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Gratuit : l'organisation l'active sans payer. Payant : il faut un abonnement.
    free: Mapped[bool] = mapped_column(default=True, nullable=False)
    price: Mapped[float] = mapped_column(default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    period: Mapped[BillingPeriod] = mapped_column(
        SqlEnum(BillingPeriod), default=BillingPeriod.MONTH, nullable=False
    )
    # Tarif Stripe du module (xpay). Nul tant que le module est gratuit.
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Retiré du rayon : plus proposé à personne, sans toucher aux organisations qui l'ont déjà.
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def payable(self) -> bool:
        """Un module payant n'est vendable que si son tarif Stripe est renseigné."""
        return not self.free and bool(self.stripe_price_id)


class ModuleSource(str, enum.Enum):
    """Pourquoi une organisation a ce module."""

    FREE = "free"      # mis en rayon gratuitement : activé sans payer
    PAID = "paid"      # abonnement xpay en cours
    GRANTED = "granted"  # accordé à la main par l'administration de la plateforme


class LicenseTenantModule(Base):
    """Un module qu'UNE organisation a pris. C'est l'entitlement réel, par organisation.

    Remplace `LicensePlan.modules` comme source de « ce que cette organisation a le droit
    d'utiliser » : un plan reste un préréglage commercial (« Pro = ces six modules »), pas la
    vérité du moment. Pendant l'essai, la question ne se pose pas — le plan d'essai ouvre `["*"]`.

    La clé primaire est le couple (organisation, module) : la base refuse d'elle-même une double
    activation, sans lecture préalable ni course possible.
    """

    __tablename__ = "license_tenant_modules"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    module_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("license_modules.code"), primary_key=True
    )
    source: Mapped[ModuleSource] = mapped_column(
        SqlEnum(ModuleSource), nullable=False, default=ModuleSource.FREE
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Nul = sans échéance (gratuit, ou accordé). Un abonnement résilié garde le module jusqu'à
    # la fin de la période payée : c'est cette date qui le dit, pas la suppression de la ligne.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Ligne d'abonnement Stripe (xpay) pour un module payant.
    stripe_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    module = relationship("LicenseModule")

    def is_current(self, now: datetime) -> bool:
        """Le module compte-t-il MAINTENANT ? (une échéance passée ne compte plus)"""
        if self.expires_at is None:
            return True
        deadline = self.expires_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline > now


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
    # Jeton JWT RS256 : ~620 caractères avec une clé RSA 2048 bits (plus avec 4096).
    # PostgreSQL applique la longueur ; 512 faisait échouer la création de toute licence.
    license_key: Mapped[str] = mapped_column(
        String(LICENSE_KEY_LENGTH), nullable=False, unique=True, index=True
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

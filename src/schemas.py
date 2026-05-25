import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class LicenseBase(BaseModel):
    tenant_id: str = Field(...,
                           description="ID du tenant associé à la licence")
    state: Optional[str] = Field(
        "trial", description="État actuel de la machine (ex: trial, active, expired)")
    extra: Optional[dict] = Field(
        None, description="Données supplémentaires associées à la licence")


class LisenseCreate(LicenseBase):

    plan_id: str = Field(...,
                         description="ID du plan de licence associé à la licence")
    expires_at: int = Field(...,
                            description="Durée de validité de la licence en jours")


class LicenseResponse(LicenseBase):
    id: uuid.UUID = Field(..., description="ID unique de la licence")
    plan_id: uuid.UUID = Field(...,
                               description="ID du plan de licence associé à la licence")
    license_key: str = Field(..., description="Clé de licence unique")
    license_hash: str = Field(...,
                              description="Hash de la licence pour vérification d'intégrité")
    issued_at: datetime = Field(...,
                                description="Date d'émission de la licence (format ISO 8601)")
    expires_at: Optional[datetime] = Field(
        None, description="Date d'expiration de la licence (format ISO 8601)")
    last_validation_at: Optional[datetime] = Field(
        None, description="Date de la dernière validation de la licence (format ISO 8601)")

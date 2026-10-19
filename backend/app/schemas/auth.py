import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: Literal["admin", "member"]
    monthly_cap_usd: Decimal
    is_active: bool
    created_at: datetime


class MeOut(BaseModel):
    user: UserOut
    month_spend_usd: Decimal
    monthly_cap_usd: Decimal
    budget_warning: bool
    budget_exceeded: bool

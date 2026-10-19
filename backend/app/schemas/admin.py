from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.schemas.auth import UserOut


class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=200)
    password: str = Field(min_length=12, max_length=1024)
    role: Literal["admin", "member"] = "member"
    monthly_cap_usd: Decimal | None = Field(default=None, ge=0)


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: Literal["admin", "member"] | None = None
    monthly_cap_usd: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=1024)


class AdminUserOut(UserOut):
    month_spend_usd: Decimal

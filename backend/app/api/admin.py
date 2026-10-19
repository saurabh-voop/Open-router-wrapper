"""Admin endpoints: users and caps (SPEC sections 11 and 12)."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.core.security import hash_password
from app.models import User
from app.schemas.admin import CreateUserRequest, UpdateUserRequest
from app.schemas.auth import UserOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
async def list_users(_: AdminUser, db: DbSession) -> list[User]:
    return list(await db.scalars(select(User).order_by(User.created_at)))


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, _: AdminUser, db: DbSession) -> User:
    email = body.email.lower()
    if await db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")
    cap = body.monthly_cap_usd
    user = User(
        email=email,
        name=body.name,
        password_hash=hash_password(body.password),
        role=body.role,
        monthly_cap_usd=cap
        if cap is not None
        else Decimal(str(get_settings().default_monthly_cap_usd)),
    )
    db.add(user)
    await db.commit()
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, body: UpdateUserRequest, admin: AdminUser, db: DbSession
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and (body.role == "member" or body.is_active is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or disable yourself")
    if body.name is not None:
        user.name = body.name
    if body.role is not None:
        user.role = body.role
    if body.monthly_cap_usd is not None:
        user.monthly_cap_usd = body.monthly_cap_usd
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    await db.commit()
    return user

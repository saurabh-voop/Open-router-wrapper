"""Shared FastAPI dependencies: DB session, current user, admin guard."""

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import SESSION_COOKIE, decode_session_token
from app.models import User

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def current_user(
    db: DbSession,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    user_id = decode_session_token(token) if token else None
    user = await db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


AdminUser = Annotated[User, Depends(admin_user)]

"""Login, logout and the current user (SPEC sections 10 and 12)."""

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.security import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    login_limiter,
    needs_rehash,
    verify_password,
)
from app.models import User
from app.schemas.auth import LoginRequest, MeOut, UserOut
from app.services.budget import budget_status

router = APIRouter(tags=["auth"])

# Verified against when the email is unknown, so response time does not reveal accounts.
_DUMMY_HASH = hash_password("relay-dummy-password")


def _cookie_secure() -> bool:
    settings = get_settings()
    return settings.is_production or settings.app_base_url.startswith("https://")


@router.post("/auth/login", response_model=UserOut)
async def login(body: LoginRequest, request: Request, response: Response, db: DbSession) -> User:
    email = body.email.lower()
    ip = request.client.host if request.client else "unknown"
    keys = (f"ip:{ip}", f"email:{email}")
    if login_limiter.is_blocked(*keys):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-in attempts. Try again later."
        )

    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    ok = verify_password(user.password_hash if user else _DUMMY_HASH, body.password)
    if user is None or not ok or not user.is_active:
        login_limiter.record_failure(*keys)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")

    login_limiter.reset(*keys)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
        await db.commit()

    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user.id),
        max_age=settings.jwt_expiry_days * 24 * 3600,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    return user


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, secure=_cookie_secure(), samesite="lax", path="/"
    )


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser, db: DbSession) -> MeOut:
    status_ = await budget_status(db, user)
    return MeOut(
        user=UserOut.model_validate(user),
        month_spend_usd=status_.spent,
        monthly_cap_usd=status_.cap,
        budget_warning=status_.warn,
        budget_exceeded=status_.exceeded,
    )

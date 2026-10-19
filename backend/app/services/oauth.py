"""Per-user Google OAuth for connectors (SPEC section 8.4).

Refresh tokens are stored Fernet-encrypted (key from SSM); access tokens are kept only in
memory until shortly before they expire.
"""

from __future__ import annotations

import contextlib
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import OAuthToken

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (endpoint, not a secret)
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
STATE_TTL = timedelta(minutes=10)
PROVIDER = "google"


class OAuthError(Exception):
    pass


def redirect_uri() -> str:
    return f"{get_settings().app_base_url.rstrip('/')}/api/connectors/google/callback"


def is_configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret and s.fernet_key)


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise OAuthError("FERNET_KEY is not configured")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as err:
        raise OAuthError("stored token cannot be decrypted (FERNET_KEY changed?)") from err


def make_state(user_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "nonce": secrets.token_urlsafe(16), "iat": now,
               "exp": now + STATE_TTL, "aud": "relay-oauth"}  # fmt: skip
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def check_state(state: str, user_id: uuid.UUID) -> None:
    try:
        payload = jwt.decode(
            state, get_settings().jwt_secret, algorithms=["HS256"], audience="relay-oauth"
        )
    except jwt.PyJWTError as err:
        raise OAuthError("invalid or expired sign-in state") from err
    if payload.get("sub") != str(user_id):
        raise OAuthError("sign-in state belongs to another user")


def auth_url(user_id: uuid.UUID, scopes: list[str]) -> str:
    params = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",  # always return a refresh token
        "state": make_state(user_id),
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code(http: httpx.AsyncClient, code: str) -> dict[str, object]:
    s = get_settings()
    resp = await http.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        },
    )
    if resp.status_code != 200:
        raise OAuthError("Google rejected the sign-in")
    data: dict[str, object] = resp.json()
    return data


async def save_token(db: AsyncSession, user_id: uuid.UUID, refresh_token: str, scopes: str) -> None:
    row = await db.scalar(
        select(OAuthToken).where(OAuthToken.user_id == user_id, OAuthToken.provider == PROVIDER)
    )
    if row is None:
        row = OAuthToken(user_id=user_id, provider=PROVIDER)
        db.add(row)
    row.encrypted_refresh_token = encrypt(refresh_token)
    row.scopes = scopes
    await db.commit()
    _cache.pop(user_id, None)


async def get_token_row(db: AsyncSession, user_id: uuid.UUID) -> OAuthToken | None:
    return await db.scalar(
        select(OAuthToken).where(OAuthToken.user_id == user_id, OAuthToken.provider == PROVIDER)
    )


async def revoke(db: AsyncSession, http: httpx.AsyncClient, user_id: uuid.UUID) -> None:
    """Revoke at Google (best effort) and always delete the stored token."""
    row = await get_token_row(db, user_id)
    if row is None:
        return
    # Best effort: deleting our copy is what matters; users can also revoke in their account.
    with contextlib.suppress(httpx.HTTPError, OAuthError):
        await http.post(GOOGLE_REVOKE_URL, data={"token": decrypt(row.encrypted_refresh_token)})
    await db.delete(row)
    await db.commit()
    _cache.pop(user_id, None)


@dataclass
class _Cached:
    token: str
    expires_at: float


_cache: dict[uuid.UUID, _Cached] = {}


async def access_token(db: AsyncSession, http: httpx.AsyncClient, user_id: uuid.UUID) -> str:
    """A valid access token for the user, refreshing it with the stored refresh token."""
    cached = _cache.get(user_id)
    if cached and cached.expires_at - 60 > time.monotonic():
        return cached.token
    row = await get_token_row(db, user_id)
    if row is None:
        raise OAuthError("Google is not connected for this user")
    s = get_settings()
    resp = await http.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "refresh_token": decrypt(row.encrypted_refresh_token),
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code != 200:
        raise OAuthError("Google access expired or was revoked; reconnect Google in Settings")
    data = resp.json()
    token = str(data["access_token"])
    _cache[user_id] = _Cached(token, time.monotonic() + float(data.get("expires_in", 3600)))
    return token

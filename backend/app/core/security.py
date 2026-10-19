"""Password hashing (argon2), session tokens (JWT) and the login rate limiter (SPEC section 10)."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

SESSION_COOKIE = "relay_session"
_JWT_ALG = "HS256"
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def create_session_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expiry_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALG)


def decode_session_token(token: str) -> uuid.UUID | None:
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[_JWT_ALG])
        return uuid.UUID(str(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


class LoginRateLimiter:
    """Failed-login limiter keyed by client IP and by email.

    In memory: the API runs as a single process on one instance (SPEC section 3).
    """

    def __init__(self, max_failures: int = 5, window_s: float = 15 * 60) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def is_blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        return any(len(self._prune(k, now)) >= self.max_failures for k in keys)

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        for k in keys:
            self._prune(k, now).append(now)

    def reset(self, *keys: str) -> None:
        for k in keys:
            self._failures.pop(k, None)


login_limiter = LoginRateLimiter()

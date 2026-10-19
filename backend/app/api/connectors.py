"""Connector accounts: per-user Google OAuth (SPEC sections 8.4 and 12)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

from app.agent.tools.connectors import get_connectors_config
from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.services import oauth

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20)


def _settings_redirect(**params: str) -> RedirectResponse:
    base = get_settings().app_base_url.rstrip("/")
    return RedirectResponse(f"{base}/settings?{urlencode(params)}", status_code=303)


@router.get("")
async def list_connectors(user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    row = await oauth.get_token_row(db, user.id)
    return [
        {
            "id": "google",
            "name": "Google (Gmail and Drive)",
            "available": oauth.is_configured() and bool(get_connectors_config().servers),
            "connected": row is not None,
            "scopes": row.scopes.split() if row else [],
        }
    ]


@router.post("/google/start")
async def start_google(user: CurrentUser) -> dict[str, str]:
    if not oauth.is_configured():
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED, "Google connectors are not configured on this server"
        )
    return {"auth_url": oauth.auth_url(user.id, get_connectors_config().google_scopes())}


@router.get("/google/callback")
async def google_callback(
    user: CurrentUser,
    db: DbSession,
    state: str = "",
    code: str = "",
    error: str = "",
) -> RedirectResponse:
    if error:
        return _settings_redirect(error="Google sign-in was cancelled")
    try:
        oauth.check_state(state, user.id)
        async with _http() as http:
            tokens = await oauth.exchange_code(http, code)
    except oauth.OAuthError as err:
        return _settings_redirect(error=str(err))
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return _settings_redirect(error="Google did not return offline access; try again")
    await oauth.save_token(db, user.id, refresh, str(tokens.get("scope") or ""))
    return _settings_redirect(connected="google")


@router.delete("/google", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_google(user: CurrentUser, db: DbSession) -> None:
    async with _http() as http:
        await oauth.revoke(db, http, user.id)

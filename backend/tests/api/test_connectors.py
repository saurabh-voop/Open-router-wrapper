"""Connectors (TASKS Phase 8): OAuth, token storage, MCP tools, approval for writes."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from mcp import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import connectors
from app.agent.tools.connectors import ConnectorsConfig, ServerConfig, ToolLists
from app.api.messages import get_tool_provider
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import OAuthToken, ToolRun, User
from app.services import oauth
from tests.api.test_chat import FakeOpenRouter, new_conversation, send, tool_call_stream
from tests.fakes import CHAT_URL, text_stream
from tests.helpers import login, make_user, parse_sse, seed_catalog

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


@pytest.fixture(autouse=True)
def google_settings() -> Iterator[None]:
    s = get_settings()
    saved = (s.google_client_id, s.google_client_secret, s.fernet_key)
    s.google_client_id, s.google_client_secret = "client-id", "client-secret"
    s.fernet_key = Fernet.generate_key().decode()
    oauth._cache.clear()
    yield
    s.google_client_id, s.google_client_secret, s.fernet_key = saved
    oauth._cache.clear()


@pytest.fixture
def fake() -> Iterator[FakeOpenRouter]:
    import respx

    f = FakeOpenRouter()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(CHAT_URL).mock(side_effect=f)
        f.router = mock
        yield f


@pytest.fixture
async def alice(client: AsyncClient, db: AsyncSession) -> User:
    await seed_catalog(db)
    user = await make_user(db, "alice@paikane.com")
    await login(client, "alice@paikane.com")
    return user


class FakeGmail:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.tokens_seen: list[str] = []
        self.server = MCPServer("fake-gmail")

        @self.server.tool()
        def search_gmail_messages(query: str) -> str:
            """Search Gmail messages."""
            return (
                f"1 message for {query}: 'Please revise the 750 kVA quote' "
                "from procurement@hindalco.example"
            )

        @self.server.tool()
        def send_gmail_message(to: str, subject: str, body: str) -> str:
            """Send an email."""
            self.sent.append({"to": to, "subject": subject, "body": body})
            return "sent"

        @self.server.tool()
        def delete_all_mail() -> str:
            """Not in connectors.yaml, must never be offered."""
            return "deleted"

    def factory(self, server: ServerConfig, token: str) -> Any:
        self.tokens_seen.append(token)

        @asynccontextmanager
        async def _open() -> AsyncIterator[Client]:
            async with Client(self.server) as c:
                yield c

        return _open()


CONFIG = ConnectorsConfig(
    servers={
        "google_workspace": ServerConfig(
            name="Google",
            provider="google",
            url="http://unused",
            scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"],
            tools=ToolLists(read=["search_gmail_messages"], write=["send_gmail_message"]),
        )
    }
)


@pytest.fixture
async def gmail(alice: User, fake: FakeOpenRouter) -> AsyncIterator[FakeGmail]:
    async with get_sessionmaker()() as db:
        await oauth.save_token(db, alice.id, "refresh-abc", "openid email gmail.readonly")
    assert fake.router is not None
    fake.router.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ya29.user-token", "expires_in": 3600}
        )
    )
    g = FakeGmail()
    provider = get_tool_provider()
    connectors.register(provider, get_sessionmaker(), CONFIG, client_factory=g.factory)
    yield g
    provider._factories.clear()


async def test_find_last_email_uses_read_tool(
    client: AsyncClient, fake: FakeOpenRouter, gmail: FakeGmail
) -> None:
    fake.category = "actions"
    fake.streams = [
        tool_call_stream(
            "search_gmail_messages", {"query": "from:hindalco"}, model="acme/allround"
        ),
        text_stream("Hindalco asked you to revise the 750 kVA quote.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "Find the last email from Hindalco")
    offered = sorted(t["function"]["name"] for t in fake.chat_bodies[0]["tools"])
    assert offered == ["search_gmail_messages", "send_gmail_message"]  # delete_all_mail blocked
    assert "approval_required" not in [e["type"] for e in events]
    assert "Please revise" in fake.chat_bodies[1]["messages"][-1]["content"]
    assert set(gmail.tokens_seen) == {"ya29.user-token"}


async def test_send_email_needs_approval_and_reject_sends_nothing(
    client: AsyncClient, db: AsyncSession, fake: FakeOpenRouter, gmail: FakeGmail
) -> None:
    fake.category = "actions"
    args = {"to": "client@x.example", "subject": "Quote", "body": "Attached."}
    fake.streams = [
        tool_call_stream("send_gmail_message", args, model="acme/allround"),
        text_stream("Okay, not sent.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "Email the quote to the client")
    approval = next(e for e in events if e["type"] == "approval_required")
    assert approval["input"] == args
    resp = await client.post(f"/tool-runs/{approval['tool_run_id']}/reject")
    assert resp.status_code == 200 and parse_sse(resp.text)[-1]["type"] == "done"
    assert gmail.sent == []
    run = await db.get(ToolRun, uuid.UUID(approval["tool_run_id"]), populate_existing=True)
    assert run is not None and run.status == "rejected"


async def test_send_email_after_approval(
    client: AsyncClient, fake: FakeOpenRouter, gmail: FakeGmail
) -> None:
    fake.category = "actions"
    args = {"to": "client@x.example", "subject": "Quote", "body": "Attached."}
    fake.streams = [
        tool_call_stream("send_gmail_message", args, model="acme/allround"),
        text_stream("Sent.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "Email the quote to the client")
    approval = next(e for e in events if e["type"] == "approval_required")
    await client.post(f"/tool-runs/{approval['tool_run_id']}/approve")
    assert gmail.sent == [args]


async def test_no_connector_tools_without_google_connection(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    g = FakeGmail()
    provider = get_tool_provider()
    connectors.register(provider, get_sessionmaker(), CONFIG, client_factory=g.factory)
    try:
        fake.category = "actions"
        conv_id = await new_conversation(client)
        await send(client, conv_id, "Find the last email from Hindalco")
    finally:
        provider._factories.clear()
    assert "tools" not in fake.chat_bodies[0]
    assert g.tokens_seen == []


# ------------------------------------------------------------------ OAuth endpoints


async def test_oauth_start_and_callback_store_encrypted_token(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    resp = await client.post("/connectors/google/start")
    assert resp.status_code == 200
    url = urlparse(resp.json()["auth_url"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["access_type"] == ["offline"] and q["client_id"] == ["client-id"]
    assert "https://www.googleapis.com/auth/drive.readonly" in q["scope"][0]
    state = q["state"][0]

    assert fake.router is not None
    token_route = fake.router.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "refresh-xyz", "scope": "openid email"}
        )
    )
    cb = await client.get("/connectors/google/callback", params={"state": state, "code": "c0de"})
    assert cb.status_code == 303 and cb.headers["location"].endswith("/settings?connected=google")
    form = parse_qs(token_route.calls[0].request.content.decode())
    assert form["code"] == ["c0de"] and form["grant_type"] == ["authorization_code"]

    raw = await db.scalar(text("select encrypted_refresh_token from oauth_tokens"))
    assert raw and "refresh-xyz" not in raw
    listed = (await client.get("/connectors")).json()
    assert listed[0]["connected"] is True


async def test_oauth_callback_rejects_state_of_another_user(
    client: AsyncClient, db: AsyncSession, alice: User
) -> None:
    other_state = oauth.make_state(uuid.uuid4())
    cb = await client.get("/connectors/google/callback", params={"state": other_state, "code": "c"})
    assert cb.status_code == 303 and "error=" in cb.headers["location"]
    assert await db.scalar(select(OAuthToken)) is None


async def test_disconnect_revokes_and_deletes_token(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    async with get_sessionmaker()() as s:
        await oauth.save_token(s, alice.id, "refresh-abc", "openid")
    bob = await make_user(db, "bob@paikane.com")
    async with get_sessionmaker()() as s:
        await oauth.save_token(s, bob.id, "refresh-bob", "openid")
    assert fake.router is not None
    revoke = fake.router.post(REVOKE_URL).mock(return_value=httpx.Response(200))
    assert (await client.delete("/connectors/google")).status_code == 204
    assert parse_qs(revoke.calls[0].request.content.decode())["token"] == ["refresh-abc"]
    owners = {
        r.user_id
        for r in await db.scalars(select(OAuthToken).execution_options(populate_existing=True))
    }
    assert owners == {bob.id}  # only Alice's token is gone


async def test_start_without_configuration_is_501(client: AsyncClient, alice: User) -> None:
    get_settings().google_client_id = ""
    assert (await client.post("/connectors/google/start")).status_code == 501


def test_shipped_connectors_yaml_parses() -> None:
    from pathlib import Path

    cfg = connectors.load_connectors_config(
        Path(__file__).resolve().parents[3] / "config" / "connectors.yaml"
    )
    server = cfg.servers["google_workspace"]
    assert "send_gmail_message" in server.tools.write
    assert not set(server.tools.read) & set(server.tools.write)
    assert json.dumps(cfg.google_scopes())

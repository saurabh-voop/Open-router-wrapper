"""Connector tools over MCP (SPEC section 8.4).

Relay is the MCP client. For each turn that has connectors enabled, it opens a session to
each configured server with the user's short-lived Google access token, lists tools, and
offers only those named in config/connectors.yaml. Write tools pause for approval (the
agent loop handles that from ``writes``). Connector results count as client data: they
stay between Relay, the MCP server and a ZDR model.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import httpx
import httpx2
import yaml
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.tools.base import ToolContext, ToolResult
from app.core.config import get_settings
from app.models import Conversation, User
from app.services import oauth

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 30_000
CALL_TIMEOUT_S = 60


class ToolLists(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    name: str
    provider: Literal["google"]
    url: str
    scopes: list[str]
    tools: ToolLists


class ConnectorsConfig(BaseModel):
    version: int = 1
    servers: dict[str, ServerConfig] = Field(default_factory=dict)

    def google_scopes(self) -> list[str]:
        scopes: dict[str, None] = {}
        for s in self.servers.values():
            if s.provider == "google":
                for scope in s.scopes:
                    scopes.setdefault(scope, None)
        return list(scopes)


def load_connectors_config(path: Path) -> ConnectorsConfig:
    if not path.exists():
        return ConnectorsConfig()
    return ConnectorsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@lru_cache
def get_connectors_config() -> ConnectorsConfig:
    return load_connectors_config(get_settings().connectors_config_path)


# Opens an MCP client for (server config, access token). Tests replace it with an
# in-process server.
ClientFactory = Callable[[ServerConfig, str], AbstractAsyncContextManager[Client]]


@asynccontextmanager
async def http_client_factory(server: ServerConfig, access_token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=httpx2.Timeout(CALL_TIMEOUT_S, connect=10),
    ) as http:
        transport = streamable_http_client(server.url, http_client=http)
        async with Client(transport, read_timeout_seconds=CALL_TIMEOUT_S) as client:
            yield client


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(f"[{getattr(item, 'type', 'content')} omitted]")
    text = "\n".join(parts) or "(no content)"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n[... truncated, {len(text)} characters total]"
    return text


class McpTool:
    def __init__(
        self,
        *,
        server: ServerConfig,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        writes: bool,
        token: Callable[[], Awaitable[str]],
        client_factory: ClientFactory,
    ) -> None:
        self.server = server
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.writes = writes
        self._token = token
        self._client_factory = client_factory

    def spec(self) -> dict[str, Any]:
        note = " This action changes data and needs the user's approval." if self.writes else ""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.server.name}] {self.description}{note}"[:1024],
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # The server takes the user from the access token; drop any identity the model sends.
        args = {k: v for k, v in args.items() if k != "user_google_email"}
        try:
            token = await self._token()
            async with self._client_factory(self.server, token) as client:
                result = await client.call_tool(self.name, args)
        except oauth.OAuthError as err:
            return ToolResult(str(err), "not connected", ok=False)
        except Exception as err:  # transport errors from the MCP SDK vary by version
            logger.warning("connector call %s failed: %s", self.name, type(err).__name__)
            return ToolResult(
                f"The {self.server.name} connector is unavailable.", "unavailable", ok=False
            )
        text = _result_text(result)
        failed = bool(getattr(result, "is_error", False))
        return ToolResult(text, f"{'error' if failed else 'ok'}, {len(text)} chars", ok=not failed)


async def list_connector_tools(
    config: ConnectorsConfig,
    token: Callable[[], Awaitable[str]],
    client_factory: ClientFactory,
) -> list[McpTool]:
    tools: list[McpTool] = []
    for server in config.servers.values():
        allowed = {n: False for n in server.tools.read} | {n: True for n in server.tools.write}
        try:
            access = await token()
            async with client_factory(server, access) as client:
                listed = await client.list_tools()
        except oauth.OAuthError:
            continue  # user has not connected this provider
        except Exception as err:
            logger.warning("connector %s unavailable: %s", server.name, type(err).__name__)
            continue
        for t in listed.tools:
            if t.name not in allowed:
                continue  # not in connectors.yaml: never offered
            tools.append(
                McpTool(
                    server=server,
                    name=t.name,
                    description=t.description or t.name,
                    input_schema=dict(t.input_schema or {}),
                    writes=allowed[t.name],
                    token=token,
                    client_factory=client_factory,
                )
            )
    return tools


def register(
    provider: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
    config: ConnectorsConfig,
    client_factory: ClientFactory = http_client_factory,
) -> None:
    http = httpx.AsyncClient(timeout=20)

    async def factory(user: User, conversation: Conversation) -> list[Any]:
        async def token() -> str:
            async with sessionmaker() as db:
                return await oauth.access_token(db, http, user.id)

        return list(await list_connector_tools(config, token, client_factory))

    provider.register("connectors", factory)

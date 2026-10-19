"""Cross-user access tests (SPEC section 10, CLAUDE.md rule 7). One per endpoint."""

import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message, ToolRun
from tests.helpers import login, make_user, seed_catalog


@dataclass
class AliceData:
    conversation_id: uuid.UUID
    answer_id: uuid.UUID
    tool_run_id: uuid.UUID


@pytest.fixture
async def alice_data(client: AsyncClient, db: AsyncSession) -> AliceData:
    await seed_catalog(db)
    alice = await make_user(db, "alice@paikane.com")
    await make_user(db, "bob@paikane.com")
    conv = Conversation(user_id=alice.id, title="Alice private")
    db.add(conv)
    await db.flush()
    q = Message(conversation_id=conv.id, role="user", content={"text": "secret"})
    db.add(q)
    await db.flush()
    step = Message(
        conversation_id=conv.id,
        role="assistant",
        parent_id=q.id,
        content={
            "final": False,
            "text": "",
            "tool_calls": [{"id": "c", "name": "x", "arguments": "{}"}],
        },
    )
    answer = Message(
        conversation_id=conv.id,
        role="assistant",
        parent_id=q.id,
        content={"final": True, "status": "awaiting_approval", "text": ""},
    )
    db.add_all([step, answer])
    await db.flush()
    run = ToolRun(
        message_id=step.id, tool_call_id="c", tool="x", input={}, status="pending_approval"
    )
    db.add(run)
    await db.commit()
    await login(client, "bob@paikane.com")
    return AliceData(conv.id, answer.id, run.id)


async def test_list_hides_other_users_conversations(
    client: AsyncClient, alice_data: AliceData
) -> None:
    assert (await client.get("/conversations")).json() == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/conversations/{c}", None),
        ("PATCH", "/conversations/{c}", {"title": "pwned"}),
        ("DELETE", "/conversations/{c}", None),
        ("POST", "/conversations/{c}/messages", {"text": "hi"}),
        ("POST", "/messages/{m}/regenerate", {}),
        ("POST", "/messages/{m}/feedback", {"rating": 1}),
        ("POST", "/tool-runs/{t}/approve", None),
        ("POST", "/tool-runs/{t}/reject", None),
    ],
)
async def test_other_users_resources_are_not_found(
    client: AsyncClient,
    db: AsyncSession,
    alice_data: AliceData,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    url = path.format(
        c=alice_data.conversation_id, m=alice_data.answer_id, t=alice_data.tool_run_id
    )
    resp = await client.request(method, url, json=body)
    assert resp.status_code == 404, (method, path, resp.status_code)
    conv = await db.get(Conversation, alice_data.conversation_id, populate_existing=True)
    assert conv is not None and conv.title == "Alice private"
    run = await db.get(ToolRun, alice_data.tool_run_id, populate_existing=True)
    assert run is not None and run.status == "pending_approval"

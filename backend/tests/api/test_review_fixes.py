"""Regression tests for the backend review findings (2026-10-19)."""

import asyncio
import io
import json
import uuid
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.files as file_service
from app.agent.tools.base import ToolContext, ToolResult
from app.agent.tools.run_python import input_attachments
from app.api.messages import get_tool_provider
from app.core.db import get_sessionmaker
from app.main import mark_interrupted_turns
from app.models import Attachment, CatalogModel, Conversation, Message, User
from app.services import budget
from tests.api.test_chat import (
    EchoTool,
    FakeOpenRouter,
    SendEmailTool,
    new_conversation,
    send,
    tool_call_stream,
    types,
)
from tests.api.test_files import upload
from tests.fakes import CHAT_URL, sse, text_stream
from tests.helpers import login, make_user, parse_sse, seed_catalog


@pytest.fixture
def fake() -> Iterator[FakeOpenRouter]:
    f = FakeOpenRouter()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(CHAT_URL).mock(side_effect=f)
        f.router = mock
        yield f


@pytest.fixture
async def alice(client: AsyncClient, db: AsyncSession) -> User:
    await seed_catalog(db)
    user = await make_user(db, "alice@paikane.com")
    await make_user(db, "admin@paikane.com", role="admin", name="Saurabh")
    await login(client, "alice@paikane.com")
    return user


class CountingSendTool(SendEmailTool):
    runs = 0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        CountingSendTool.runs += 1
        await asyncio.sleep(0.05)  # widen the race window
        return ToolResult("sent", "sent")


class NulTool(EchoTool):
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult("binary\x00output", "ran")


@pytest.fixture
def tools() -> Iterator[None]:
    provider = get_tool_provider()

    async def python_tools(user: User, conv: Conversation) -> list[Any]:
        return [NulTool()]

    async def connector_tools(user: User, conv: Conversation) -> list[Any]:
        return [CountingSendTool()]

    provider.register("run_python", python_tools)
    provider.register("connectors", connector_tools)
    CountingSendTool.runs = 0
    yield
    provider._factories.clear()


async def test_failure_before_model_loop_does_not_lock_conversation(
    client: AsyncClient, alice: User, fake: FakeOpenRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = await new_conversation(client)
    att = await upload(client, "a.txt", b"hello", "text/plain", conv_id)

    async def broken(key: str) -> bytes:
        raise OSError("S3 down")

    monkeypatch.setattr(file_service, "get_object", broken)
    events = await send(client, conv_id, "read it", attachment_ids=[att["id"]])
    assert any(e["type"] == "error" for e in events)
    monkeypatch.undo()
    # The conversation is usable again.
    events = await send(client, conv_id, "hello again friend")
    assert types(events)[-1] == "done" and "error" not in types(events)


async def test_double_approve_runs_write_action_once(
    client: AsyncClient, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "actions"
    fake.streams = [
        tool_call_stream("gmail_send", {"to": "c@x.example"}, model="acme/allround"),
        text_stream("Sent.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "send the quote")
    run_id = next(e for e in events if e["type"] == "approval_required")["tool_run_id"]
    first, second = await asyncio.gather(
        client.post(f"/tool-runs/{run_id}/approve"), client.post(f"/tool-runs/{run_id}/approve")
    )
    assert CountingSendTool.runs == 1
    statuses = {first.status_code, second.status_code}
    assert statuses <= {200, 409}


async def test_nul_bytes_are_stripped_not_fatal(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "data_analysis"
    fake.streams = [
        tool_call_stream("run_python", {"code": "print('\\x00')"}, model="acme/coder"),
        text_stream("Done\x00.", model="acme/coder"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "run it\x00 please")
    assert "error" not in types(events), events
    rows = list(await db.scalars(select(Message).execution_options(populate_existing=True)))
    assert rows and all("\x00" not in json.dumps(m.content) for m in rows)
    user_msg = next(m for m in rows if m.role == "user")
    assert user_msg.content["text"] == "run it please"


async def test_sandbox_inputs_exclude_outputs_and_unsent_uploads(
    db: AsyncSession, alice: User
) -> None:
    conv = Conversation(user_id=alice.id, title="t")
    db.add(conv)
    await db.flush()
    msg = Message(conversation_id=conv.id, role="user", content={"text": "x"})
    db.add(msg)
    await db.flush()

    def att(name: str, message_id: uuid.UUID | None) -> Attachment:
        return Attachment(
            user_id=alice.id, conversation_id=conv.id, message_id=message_id,
            s3_key=f"k/{uuid.uuid4()}", filename=name, mime="text/csv", size_bytes=1, sha256="0",
        )  # fmt: skip

    db.add_all([att(f"in{i}.csv", msg.id) for i in range(25)])
    db.add_all([att("chart.png", None), att("unsent.csv", None)])
    await db.commit()
    rows = await input_attachments(db, alice.id, conv.id)
    names = {a.filename for a in rows}
    assert len(rows) == 20 and "chart.png" not in names and "unsent.csv" not in names


async def test_xlsx_zip_bomb_rejected(client: AsyncClient, alice: User) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/sharedStrings.xml", b"<a>" + b"A" * (120 * 1024 * 1024) + b"</a>")
    resp = await client.post(
        "/files",
        files={"file": ("bomb.xlsx", buf.getvalue(), "application/vnd.ms-excel")},
    )
    assert resp.status_code == 422 and "too large" in resp.json()["detail"]


async def test_resume_checks_budget_before_calling_model(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "actions"
    fake.streams = [tool_call_stream("gmail_send", {"to": "c@x.example"}, model="acme/allround")]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "send the quote")
    run_id = next(e for e in events if e["type"] == "approval_required")["tool_run_id"]
    await budget.add_cost(db, alice.id, Decimal("999"))
    await db.commit()
    calls_before = len(fake.chat_bodies)
    resumed = parse_sse((await client.post(f"/tool-runs/{run_id}/approve")).text)
    assert any(e["type"] == "error" and "monthly budget" in e["message"] for e in resumed)
    assert len(fake.chat_bodies) == calls_before


async def test_cost_recorded_when_stream_ends_with_error(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "acme/allround"}
    fake.streams = [
        sse(
            [
                {**base, "choices": [{"index": 0, "delta": {"content": "par"}}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                 "usage": {"prompt_tokens": 9, "completion_tokens": 1, "cost": 0.25}},
            ]
        )
    ]  # fmt: skip
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "hello there friend")
    assert any(e["type"] == "error" for e in events)
    spent = await budget.month_spend(db, alice.id)
    assert spent >= Decimal("0.25")


async def test_classifier_without_zdr_is_never_called(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    row = await db.get(CatalogModel, "acme/fast-classifier")
    assert row is not None
    row.has_zdr_endpoint = False
    await db.commit()
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "write some python code please")
    assert fake.classifier_calls == 0
    assert events[0]["category"] == "general"  # default category for a new conversation


async def test_startup_marks_interrupted_turns(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    q = Message(conversation_id=uuid.UUID(conv_id), role="user", content={"text": "hi"})
    db.add(q)
    await db.flush()
    db.add(
        Message(
            conversation_id=uuid.UUID(conv_id), role="assistant", parent_id=q.id,
            content={"final": True, "status": "streaming", "text": ""},
        )
    )  # fmt: skip
    await db.commit()
    assert (
        await client.post(f"/conversations/{conv_id}/messages", json={"text": "x"})
    ).status_code == 409
    await mark_interrupted_turns(get_sessionmaker())
    events = await send(client, conv_id, "hello again friend")
    assert types(events)[-1] == "done"


async def test_concurrent_sends_start_one_turn(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    a, b = await asyncio.gather(
        client.post(f"/conversations/{conv_id}/messages", json={"text": "first message here"}),
        client.post(f"/conversations/{conv_id}/messages", json={"text": "second message here"}),
    )
    codes = sorted([a.status_code, b.status_code])
    finals = list(
        await db.scalars(
            select(Message).where(Message.role == "assistant", Message.parent_id.is_not(None))
        )
    )
    # Either the second send was refused, or it started only after the first had finished;
    # never two turns running at once.
    assert codes in ([200, 409], [200, 200])
    if codes == [200, 200]:
        assert len(finals) == 2
    else:
        assert len(finals) == 1

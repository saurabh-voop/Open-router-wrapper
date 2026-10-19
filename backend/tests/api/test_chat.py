"""End-to-end chat tests through the API with OpenRouter mocked (respx)."""

import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import ToolContext, ToolResult
from app.api.messages import get_tool_provider
from app.models import Conversation, Feedback, Message, RoutingDecision, ToolRun, UsageLedger, User
from app.services import budget
from tests.fakes import CHAT_URL, sse, sse_response, text_stream
from tests.helpers import (
    classifier_response,
    is_classifier,
    login,
    make_user,
    parse_sse,
    seed_catalog,
)


class FakeOpenRouter:
    """Dispatches mocked OpenRouter calls: classifier vs. chat streams queued in order."""

    def __init__(self, category: str = "general", confidence: float = 0.9) -> None:
        self.category = category
        self.confidence = confidence
        self.needs: list[str] = []
        self.streams: list[str] = []
        self.chat_bodies: list[dict[str, Any]] = []
        self.classifier_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if is_classifier(request):
            self.classifier_calls += 1
            return classifier_response(self.category, self.confidence, self.needs)
        self.chat_bodies.append(json.loads(request.content))
        body = self.streams.pop(0) if self.streams else text_stream("Default answer.")
        return sse_response(body)


@pytest.fixture
def fake() -> Iterator[FakeOpenRouter]:
    f = FakeOpenRouter()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(CHAT_URL).mock(side_effect=f)
        yield f


@pytest.fixture
async def alice(client: AsyncClient, db: AsyncSession) -> User:
    await seed_catalog(db)
    user = await make_user(db, "alice@paikane.com", name="Alice")
    await make_user(db, "admin@paikane.com", role="admin", name="Saurabh")
    await login(client, "alice@paikane.com")
    return user


async def new_conversation(client: AsyncClient, **body: Any) -> str:
    resp = await client.post("/conversations", json=body)
    assert resp.status_code == 201
    return str(resp.json()["id"])


async def send(client: AsyncClient, conv_id: str, text: str, **body: Any) -> list[dict[str, Any]]:
    resp = await client.post(f"/conversations/{conv_id}/messages", json={"text": text, **body})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


# ------------------------------------------------------------------ basic flow


async def test_send_message_streams_routes_and_persists(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "coding"
    fake.streams = [text_stream("Use a list comprehension.", model="acme/coder", cost=0.0021)]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "How do I flatten a list in Python?")

    t = types(events)
    assert t[0] == "route"
    assert t.index("route") < t.index("token")
    assert t[-2:] == ["usage", "done"]
    route_ev = events[0]
    assert route_ev["model"] == "acme/coder"
    assert route_ev["category"] == "coding"
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Use a list comprehension."

    body = fake.chat_bodies[0]
    assert body["model"] == "acme/coder"
    assert body["models"] == ["acme/allround", "acme/vision-pro"]
    assert body["provider"] == {"zdr": True, "data_collection": "deny"}
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"role": "user", "content": "How do I flatten a list in Python?"}

    final = await db.scalar(
        select(Message).where(Message.role == "assistant").execution_options(populate_existing=True)
    )
    assert final is not None
    assert final.model == "acme/coder"
    assert final.cost_usd == Decimal("0.0021")
    assert (final.prompt_tokens, final.completion_tokens, final.cached_tokens) == (10, 5, 2)
    decision = await db.scalar(select(RoutingDecision))
    assert decision is not None and decision.message_id == final.id
    assert decision.chosen_model == "acme/coder" and decision.answered_model == "acme/coder"
    assert decision.category == "coding"
    spent = await budget.month_spend(db, alice.id)
    assert spent == Decimal("0.0021") + Decimal("0.0001")  # answer + classifier

    conv = await db.get(Conversation, uuid.UUID(conv_id), populate_existing=True)
    assert conv is not None
    assert conv.current_model == "acme/coder" and conv.current_category == "coding"
    assert conv.title == "How do I flatten a list in Python?"


async def test_answered_model_from_fallback_is_stored(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "coding"
    fake.streams = [text_stream("ok", model="acme/allround")]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "Fix my endpoint")
    assert events[0]["model"] == "acme/coder"
    detail = (await client.get(f"/conversations/{conv_id}")).json()
    answer = detail["messages"][-1]
    assert answer["model"] == "acme/allround"
    assert answer["routing"]["chosen_model"] == "acme/coder"
    assert answer["routing"]["answered_model"] == "acme/allround"


async def test_history_survives_reload(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    fake.streams = [
        text_stream("First answer.", model="acme/allround"),
        text_stream("Second answer.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    await send(client, conv_id, "hello there")
    await send(client, conv_id, "tell me more about gensets please")
    detail = (await client.get(f"/conversations/{conv_id}")).json()
    msgs = [(m["role"], m["text"]) for m in detail["messages"]]
    assert msgs == [
        ("user", "hello there"),
        ("assistant", "First answer."),
        ("user", "tell me more about gensets please"),
        ("assistant", "Second answer."),
    ]
    assert detail["messages"][1]["model"] == "acme/allround"
    # Second call carried the first exchange as history.
    roles = [m["role"] for m in fake.chat_bodies[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


async def test_switch_notice_in_route_event(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    fake.category = "writing"
    await send(client, conv_id, "Draft an email to the Hindalco team about the AMC renewal")
    fake.category = "coding"
    events = await send(client, conv_id, "Now write a SQL query for overdue AMC invoices")
    assert events[0]["switched"] is True
    assert events[0]["model"] == "acme/coder"


async def test_second_send_while_answering_is_rejected(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    user_msg = Message(conversation_id=uuid.UUID(conv_id), role="user", content={"text": "hi"})
    db.add(user_msg)
    await db.flush()
    db.add(
        Message(
            conversation_id=uuid.UUID(conv_id),
            role="assistant",
            parent_id=user_msg.id,
            content={"final": True, "status": "streaming", "text": ""},
        )
    )
    await db.commit()
    resp = await client.post(f"/conversations/{conv_id}/messages", json={"text": "again"})
    assert resp.status_code == 409


# ------------------------------------------------------------------ overrides


async def test_regenerate_with_model_records_override(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "writing"
    fake.streams = [text_stream("Draft one."), text_stream("Draft two.", model="acme/thinker")]
    conv_id = await new_conversation(client)
    await send(client, conv_id, "Write a follow-up email to a client about a tender")
    first = (await client.get(f"/conversations/{conv_id}")).json()["messages"][-1]

    calls_before = fake.classifier_calls
    resp = await client.post(f"/messages/{first['id']}/regenerate", json={"model": "acme/thinker"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[0]["model"] == "acme/thinker"
    assert fake.classifier_calls == calls_before  # reuses the earlier classification

    detail = (await client.get(f"/conversations/{conv_id}")).json()
    assert [m["text"] for m in detail["messages"]] == [
        "Write a follow-up email to a client about a tender",
        "Draft two.",
    ]
    old_decision = await db.scalar(
        select(RoutingDecision)
        .where(RoutingDecision.message_id == uuid.UUID(first["id"]))
        .execution_options(populate_existing=True)
    )
    assert old_decision is not None
    assert old_decision.regenerated is True and old_decision.overridden_to == "acme/thinker"


async def test_regenerate_rejects_disallowed_model(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    await send(client, conv_id, "hello there friend")
    answer = (await client.get(f"/conversations/{conv_id}")).json()["messages"][-1]
    resp = await client.post(f"/messages/{answer['id']}/regenerate", json={"model": "other/no-zdr"})
    assert resp.status_code == 422


async def test_lock_model_is_used_for_next_message(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    resp = await client.patch(f"/conversations/{conv_id}", json={"locked_model": "acme/thinker"})
    assert resp.status_code == 200 and resp.json()["locked_model"] == "acme/thinker"
    fake.category = "coding"
    events = await send(client, conv_id, "write some python")
    assert events[0]["model"] == "acme/thinker"
    assert fake.chat_bodies[0]["model"] == "acme/thinker"
    assert "models" not in fake.chat_bodies[0]

    bad = await client.patch(f"/conversations/{conv_id}", json={"locked_model": "other/no-zdr"})
    assert bad.status_code == 422
    unlock = await client.patch(f"/conversations/{conv_id}", json={"locked_model": None})
    assert unlock.json()["locked_model"] is None


async def test_feedback_is_stored_and_updated(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    conv_id = await new_conversation(client)
    await send(client, conv_id, "hello there friend")
    answer = (await client.get(f"/conversations/{conv_id}")).json()["messages"][-1]
    assert (
        await client.post(f"/messages/{answer['id']}/feedback", json={"rating": 1})
    ).status_code == 204
    await client.post(f"/messages/{answer['id']}/feedback", json={"rating": -1, "comment": "wrong"})
    rows = list(await db.scalars(select(Feedback).execution_options(populate_existing=True)))
    assert len(rows) == 1 and rows[0].rating == -1 and rows[0].comment == "wrong"
    detail = (await client.get(f"/conversations/{conv_id}")).json()
    assert detail["messages"][-1]["rating"] == -1


# ------------------------------------------------------------------ privacy and tools


async def test_confidential_conversation_never_sends_web_search(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "research"
    open_id = await new_conversation(client)
    await send(client, open_id, "latest diesel genset prices in Pune", web_search=True)
    assert {
        "type": "openrouter:web_search",
        "parameters": {"max_results": 5, "max_total_results": 15},
    } in fake.chat_bodies[0]["tools"]

    secret_id = await new_conversation(client, confidential=True)
    await send(client, secret_id, "latest diesel genset prices in Pune", web_search=True)
    body = fake.chat_bodies[1]
    assert not any(t.get("type") == "openrouter:web_search" for t in body.get("tools") or [])


async def test_citations_are_stored_and_emitted(
    client: AsyncClient, alice: User, fake: FakeOpenRouter
) -> None:
    fake.category = "research"
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "acme/searcher"}
    cite = {"type": "url_citation", "url_citation": {"url": "https://news.example/a", "title": "A"}}
    fake.streams = [
        sse(
            [
                {**base, "choices": [{"index": 0, "delta": {"content": "Prices rose [1]."}}]},
                {**base, "choices": [{"index": 0, "delta": {"annotations": [cite, cite]},
                                      "finish_reason": "stop"}]},
            ]
        )
    ]  # fmt: skip
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "what happened to steel prices this week")
    cites = [e for e in events if e["type"] == "citation"]
    assert cites == [
        {"type": "citation", "index": 1, "url": "https://news.example/a", "title": "A"}
    ]
    answer = (await client.get(f"/conversations/{conv_id}")).json()["messages"][-1]
    assert answer["citations"] == [{"index": 1, "url": "https://news.example/a", "title": "A"}]


class EchoTool:
    name = "run_python"
    writes = False

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": "Run Python",
                "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=f"stdout: ran {args.get('code')}", summary="ran code")


class SendEmailTool(EchoTool):
    name = "gmail_send"
    writes = True

    def spec(self) -> dict[str, Any]:
        spec = super().spec()
        spec["function"]["name"] = "gmail_send"
        return spec


def tool_call_stream(
    name: str, args: dict[str, Any], *, model: str, call_id: str = "call_1"
) -> str:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": model}
    return sse(
        [
            {**base, "choices": [{"index": 0, "delta": {"content": "Running it."}}]},
            {**base, "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.001}},
        ]
    )  # fmt: skip


@pytest.fixture
def tools() -> Iterator[None]:
    provider = get_tool_provider()

    async def python_tools(user: User, conv: Conversation) -> list[Any]:
        return [EchoTool()]

    async def connector_tools(user: User, conv: Conversation) -> list[Any]:
        return [SendEmailTool()]

    provider.register("run_python", python_tools)
    provider.register("connectors", connector_tools)
    yield
    provider._factories.clear()


async def test_tool_loop_stays_on_answered_model(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "data_analysis"
    fake.streams = [
        tool_call_stream("run_python", {"code": "print(1)"}, model="acme/allround"),
        text_stream("The result is 1.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "compute the total of column B")
    t = types(events)
    assert "tool_start" in t and "tool_end" in t
    first, second = fake.chat_bodies
    assert first["model"] == "acme/coder" and first["models"] == ["acme/allround"]
    # The fallback answered; the loop must not switch back or offer other models.
    assert second["model"] == "acme/allround" and "models" not in second
    assert second["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "stdout: ran print(1)",
    }
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "run_python"
    run = await db.scalar(select(ToolRun))
    assert run is not None and run.status == "succeeded" and run.tool == "run_python"

    detail = (await client.get(f"/conversations/{conv_id}")).json()
    answer = detail["messages"][-1]
    assert answer["text"] == "Running it.\n\nThe result is 1."
    assert [r["tool"] for r in answer["tool_runs"]] == ["run_python"]


async def test_tool_loop_is_capped(
    client: AsyncClient, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "data_analysis"
    fake.streams = [
        tool_call_stream("run_python", {"code": str(i)}, model="acme/coder", call_id=f"c{i}")
        for i in range(9)
    ] + [text_stream("Done after many steps.", model="acme/coder")]
    conv_id = await new_conversation(client)
    await send(client, conv_id, "keep running code")
    assert len(fake.chat_bodies) == 10
    assert fake.chat_bodies[-1]["tool_choice"] == "none"


async def test_write_tool_requires_approval_and_reject_sends_nothing(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "actions"
    fake.streams = [
        tool_call_stream("gmail_send", {"to": "client@x.com"}, model="acme/allround"),
        text_stream("OK, I did not send it.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "send the quote to the client")
    approval = next(e for e in events if e["type"] == "approval_required")
    assert approval["tool"] == "gmail_send" and approval["input"] == {"to": "client@x.com"}
    assert "tool_start" not in types(events)

    # While waiting, new messages are refused.
    resp = await client.post(f"/conversations/{conv_id}/messages", json={"text": "hi"})
    assert resp.status_code == 409

    resp = await client.post(f"/tool-runs/{approval['tool_run_id']}/reject")
    assert resp.status_code == 200
    resumed = parse_sse(resp.text)
    assert "tool_start" not in types(resumed)
    assert types(resumed)[-1] == "done"
    run = await db.get(ToolRun, uuid.UUID(approval["tool_run_id"]), populate_existing=True)
    assert run is not None and run.status == "rejected" and run.approved_by is None
    tool_msg = fake.chat_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "rejected" in tool_msg["content"]
    again = await client.post(f"/tool-runs/{approval['tool_run_id']}/approve")
    assert again.status_code == 409


async def test_write_tool_runs_after_approval(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    fake.category = "actions"
    fake.streams = [
        tool_call_stream("gmail_send", {"to": "client@x.com"}, model="acme/allround"),
        text_stream("Sent.", model="acme/allround"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "send the quote to the client")
    approval = next(e for e in events if e["type"] == "approval_required")
    resp = await client.post(f"/tool-runs/{approval['tool_run_id']}/approve")
    resumed = parse_sse(resp.text)
    assert "tool_start" in types(resumed)
    run = await db.get(ToolRun, uuid.UUID(approval["tool_run_id"]), populate_existing=True)
    assert run is not None and run.status == "succeeded" and run.approved_by == alice.id
    detail = (await client.get(f"/conversations/{conv_id}")).json()
    assert detail["messages"][-1]["text"] == "Running it.\n\nSent."


# ------------------------------------------------------------------ budgets


async def test_user_at_cap_is_blocked_before_any_model_call(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter
) -> None:
    db.add(UsageLedger(user_id=alice.id, month=budget.current_month(), cost_usd=Decimal("50")))
    await db.commit()
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "hello there friend")
    err = next(e for e in events if e["type"] == "error")
    assert "monthly budget of $50.00" in err["message"] and "Saurabh" in err["message"]
    assert fake.chat_bodies == [] and fake.classifier_calls == 0


async def test_tool_loop_stops_when_cap_crossed(
    client: AsyncClient, db: AsyncSession, alice: User, fake: FakeOpenRouter, tools: None
) -> None:
    alice.monthly_cap_usd = Decimal("0.001")
    await db.commit()
    fake.category = "data_analysis"
    fake.streams = [
        tool_call_stream("run_python", {"code": "1"}, model="acme/coder"),
        text_stream("should not be reached"),
    ]
    conv_id = await new_conversation(client)
    events = await send(client, conv_id, "crunch the numbers")
    assert any(e["type"] == "error" and "monthly budget" in e["message"] for e in events)
    assert len(fake.chat_bodies) == 1


async def test_me_reports_spend_and_warning(
    client: AsyncClient, db: AsyncSession, alice: User
) -> None:
    db.add(UsageLedger(user_id=alice.id, month=budget.current_month(), cost_usd=Decimal("41")))
    await db.commit()
    me = (await client.get("/me")).json()
    assert Decimal(me["month_spend_usd"]) == Decimal("41")
    assert me["budget_warning"] is True and me["budget_exceeded"] is False

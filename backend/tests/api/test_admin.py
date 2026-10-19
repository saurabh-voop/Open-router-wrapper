"""Admin endpoints (SPEC 11, 13) and the "admin raises cap, user continues" flow."""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Feedback, Message, RoutingDecision, UsageLedger, User
from app.services import budget
from tests.helpers import login, make_user


@pytest.fixture
async def setup(client: AsyncClient, db: AsyncSession) -> tuple[User, User]:
    admin = await make_user(db, "admin@paikane.com", role="admin", name="Saurabh")
    member = await make_user(db, "asha@paikane.com", name="Asha", cap="5")
    month = budget.current_month()
    db.add(UsageLedger(user_id=member.id, month=month, cost_usd=Decimal("5")))
    conv = Conversation(user_id=member.id, title="t")
    db.add(conv)
    await db.flush()
    q = Message(conversation_id=conv.id, role="user", content={"text": "q"})
    db.add(q)
    await db.flush()
    a = Message(
        conversation_id=conv.id,
        role="assistant",
        parent_id=q.id,
        content={"final": True, "status": "done", "text": "a"},
        model="acme/coder",
        cost_usd=Decimal("4.5"),
    )
    db.add(a)
    await db.flush()
    db.add(
        RoutingDecision(
            message_id=a.id,
            category="coding",
            confidence=0.9,
            chosen_model="acme/coder",
            answered_model="acme/coder",
            overridden_to="acme/thinker",
            regenerated=True,
            reason="looks like coding",
        )
    )
    db.add(Feedback(message_id=a.id, user_id=member.id, rating=-1))
    await db.commit()
    return admin, member


async def test_admin_endpoints_require_admin(client: AsyncClient, setup: tuple[User, User]) -> None:
    await login(client, "asha@paikane.com")
    for path in ["/admin/users", "/admin/usage", "/admin/routing", "/admin/catalog"]:
        assert (await client.get(path)).status_code == 403, path


async def test_users_include_month_spend(client: AsyncClient, setup: tuple[User, User]) -> None:
    await login(client, "admin@paikane.com")
    users = {u["email"]: u for u in (await client.get("/admin/users")).json()}
    assert Decimal(users["asha@paikane.com"]["month_spend_usd"]) == Decimal("5")
    assert Decimal(users["admin@paikane.com"]["month_spend_usd"]) == 0


async def test_usage_per_user_model_and_category(
    client: AsyncClient, setup: tuple[User, User]
) -> None:
    await login(client, "admin@paikane.com")
    body = (await client.get("/admin/usage")).json()
    assert body["month"] == budget.current_month()
    assert [(u["email"], Decimal(u["cost_usd"])) for u in body["per_user"]] == [
        ("asha@paikane.com", Decimal("5"))
    ]
    models = {m["model"]: Decimal(m["cost_usd"]) for m in body["per_model"]}
    assert models == {"acme/coder": Decimal("4.5"), "router classifier": Decimal("0.5")}
    assert body["categories"] == [{"category": "coding", "count": 1}]
    assert (await client.get("/admin/usage?month=2026-13")).status_code == 422
    empty = (await client.get("/admin/usage?month=2020-01")).json()
    assert empty["per_user"] == [] and empty["per_model"] == []


async def test_routing_table_shows_overrides_and_rating(
    client: AsyncClient, setup: tuple[User, User]
) -> None:
    await login(client, "admin@paikane.com")
    rows = (await client.get("/admin/routing")).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["user_email"] == "asha@paikane.com"
    assert row["overridden_to"] == "acme/thinker" and row["regenerated"] is True
    assert row["rating"] == -1 and row["category"] == "coding"


async def test_catalog_status_lists_operational_warnings(
    client: AsyncClient, setup: tuple[User, User]
) -> None:
    await login(client, "admin@paikane.com")
    body = (await client.get("/admin/catalog")).json()
    assert any("virus" in w for w in body["warnings"])


async def test_admin_raises_cap_and_user_continues(
    client: AsyncClient, db: AsyncSession, setup: tuple[User, User]
) -> None:
    _, member = setup
    await login(client, "asha@paikane.com")
    assert (await client.get("/me")).json()["budget_exceeded"] is True
    await login(client, "admin@paikane.com")
    resp = await client.patch(f"/admin/users/{member.id}", json={"monthly_cap_usd": "20"})
    assert resp.status_code == 200
    await login(client, "asha@paikane.com")
    me = (await client.get("/me")).json()
    assert me["budget_exceeded"] is False and Decimal(me["monthly_cap_usd"]) == 20


async def test_admin_cannot_demote_self(client: AsyncClient, setup: tuple[User, User]) -> None:
    admin, _ = setup
    await login(client, "admin@paikane.com")
    resp = await client.patch(f"/admin/users/{admin.id}", json={"role": "member"})
    assert resp.status_code == 400
    assert (
        await client.patch(f"/admin/users/{uuid.uuid4()}", json={"name": "x"})
    ).status_code == 404

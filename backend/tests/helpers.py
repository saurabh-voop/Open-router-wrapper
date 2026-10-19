"""Shared helpers for API tests."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.llm.catalog import model_rows
from app.models import CatalogModel, User
from tests.fakes import FAKE_MODELS, FAKE_ZDR, json_completion

PASSWORD = "correct horse battery staple"


async def make_user(
    db: AsyncSession,
    email: str,
    *,
    role: str = "member",
    cap: str = "50",
    name: str = "",
) -> User:
    user = User(
        email=email,
        name=name,
        password_hash=hash_password(PASSWORD),
        role=role,
        monthly_cap_usd=Decimal(cap),
    )
    db.add(user)
    await db.commit()
    return user


async def login(client: AsyncClient, email: str) -> None:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text


async def seed_catalog(db: AsyncSession) -> None:
    for row in model_rows(FAKE_MODELS, FAKE_ZDR, datetime.now(UTC)):
        db.add(CatalogModel(**row))
    await db.commit()


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def classifier_response(
    category: str, confidence: float = 0.9, needs: list[str] | None = None
) -> httpx.Response:
    payload = {
        "category": category,
        "confidence": confidence,
        "is_followup": False,
        "needs": needs or [],
        "reason": f"looks like {category}",
    }
    return httpx.Response(
        200, json=json_completion(json.dumps(payload), model="acme/fast-classifier")
    )


def is_classifier(request: httpx.Request) -> bool:
    return bool(json.loads(request.content).get("model") == "acme/fast-classifier")

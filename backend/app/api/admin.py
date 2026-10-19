"""Admin endpoints: users and caps, spend, routing decisions, catalog (SPEC 7, 11, 12, 13)."""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, select

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.core.security import hash_password
from app.llm import catalog
from app.models import Conversation, Feedback, Message, RoutingDecision, UsageLedger, User
from app.schemas.admin import AdminUserOut, CreateUserRequest, UpdateUserRequest
from app.schemas.auth import UserOut
from app.services.budget import current_month

router = APIRouter(prefix="/admin", tags=["admin"])

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(x) for x in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return start, end


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(_: AdminUser, db: DbSession) -> list[AdminUserOut]:
    month = current_month()
    rows = await db.execute(
        select(User, UsageLedger.cost_usd)
        .outerjoin(UsageLedger, and_(UsageLedger.user_id == User.id, UsageLedger.month == month))
        .order_by(User.created_at)
    )
    return [
        AdminUserOut(**UserOut.model_validate(u).model_dump(), month_spend_usd=spent or Decimal(0))
        for u, spent in rows
    ]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, _: AdminUser, db: DbSession) -> User:
    email = body.email.lower()
    if await db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")
    cap = body.monthly_cap_usd
    user = User(
        email=email,
        name=body.name,
        password_hash=hash_password(body.password),
        role=body.role,
        monthly_cap_usd=cap
        if cap is not None
        else Decimal(str(get_settings().default_monthly_cap_usd)),
    )
    db.add(user)
    await db.commit()
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, body: UpdateUserRequest, admin: AdminUser, db: DbSession
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and (body.role == "member" or body.is_active is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or disable yourself")
    if body.name is not None:
        user.name = body.name
    if body.role is not None:
        user.role = body.role
    if body.monthly_cap_usd is not None:
        user.monthly_cap_usd = body.monthly_cap_usd
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    await db.commit()
    return user


@router.get("/usage")
async def usage(
    _: AdminUser,
    db: DbSession,
    month: Annotated[str | None, Query(pattern=MONTH_RE.pattern)] = None,
) -> dict[str, Any]:
    month = month or current_month()
    start, end = _month_bounds(month)
    per_user = [
        {"user_id": str(uid), "email": email, "name": name, "cost_usd": str(cost)}
        for uid, email, name, cost in await db.execute(
            select(User.id, User.email, User.name, UsageLedger.cost_usd)
            .join(UsageLedger, UsageLedger.user_id == User.id)
            .where(UsageLedger.month == month)
            .order_by(UsageLedger.cost_usd.desc())
        )
    ]
    model_rows = list(
        await db.execute(
            select(Message.model, func.coalesce(func.sum(Message.cost_usd), 0), func.count())
            .where(
                Message.role == "assistant",
                Message.model.is_not(None),
                Message.created_at >= start,
                Message.created_at < end,
            )
            .group_by(Message.model)
            .order_by(func.sum(Message.cost_usd).desc().nulls_last())
        )
    )
    per_model: list[dict[str, Any]] = [
        {"model": m, "cost_usd": str(c), "messages": n} for m, c, n in model_rows
    ]
    ledger_total = sum((Decimal(r["cost_usd"]) for r in per_user), Decimal(0))
    model_total = sum((Decimal(str(c)) for _, c, _ in model_rows), Decimal(0))
    if ledger_total > model_total:
        # Classifier calls are charged to the ledger but not stored as messages.
        per_model.append(
            {
                "model": "router classifier",
                "cost_usd": str(ledger_total - model_total),
                "messages": 0,
            }
        )
    categories = [
        {"category": c, "count": n}
        for c, n in await db.execute(
            select(RoutingDecision.category, func.count())
            .where(RoutingDecision.created_at >= start, RoutingDecision.created_at < end)
            .group_by(RoutingDecision.category)
            .order_by(func.count().desc())
        )
    ]
    return {"month": month, "per_user": per_user, "per_model": per_model, "categories": categories}


@router.get("/routing")
async def routing(
    _: AdminUser, db: DbSession, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(RoutingDecision, User.email, Feedback.rating)
        .join(Message, Message.id == RoutingDecision.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(User, User.id == Conversation.user_id)
        .outerjoin(Feedback, and_(Feedback.message_id == Message.id, Feedback.user_id == User.id))
        .order_by(RoutingDecision.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(d.id),
            "created_at": d.created_at.isoformat(),
            "user_email": email,
            "category": d.category,
            "confidence": d.confidence,
            "chosen_model": d.chosen_model,
            "answered_model": d.answered_model,
            "switched": d.switched,
            "forced": d.forced,
            "reason": d.reason,
            "overridden_to": d.overridden_to,
            "regenerated": d.regenerated,
            "classifier_latency_ms": d.classifier_latency_ms,
            "rating": rating,
        }
        for d, email, rating in rows
    ]


@router.get("/catalog")
async def catalog_status(_: AdminUser) -> dict[str, Any]:
    report = catalog.last_report
    settings = get_settings()
    warnings = list(report.warnings)
    if not settings.clamav_host:
        warnings.append("Uploads are type-checked but not virus-scanned (CLAMAV_HOST not set).")
    if not settings.sandbox_url:
        warnings.append("Code execution is off (SANDBOX_URL not set).")
    return {
        "synced_at": report.synced_at.isoformat() if report.synced_at else None,
        "model_count": report.model_count,
        "zdr_model_count": report.zdr_model_count,
        "warnings": warnings,
        "error": report.error,
    }

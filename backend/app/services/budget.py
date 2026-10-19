"""Monthly spend tracking and caps (SPEC section 11)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageLedger, User

WARN_FRACTION = Decimal("0.8")


def current_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


async def month_spend(db: AsyncSession, user_id: uuid.UUID, month: str | None = None) -> Decimal:
    value = await db.scalar(
        select(UsageLedger.cost_usd).where(
            UsageLedger.user_id == user_id, UsageLedger.month == (month or current_month())
        )
    )
    return value if value is not None else Decimal(0)


async def add_cost(db: AsyncSession, user_id: uuid.UUID, cost: Decimal) -> None:
    """Atomically add ``cost`` to this month's ledger row. Caller commits."""
    if cost <= 0:
        return
    stmt = insert(UsageLedger).values(user_id=user_id, month=current_month(), cost_usd=cost)
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageLedger.user_id, UsageLedger.month],
        set_={"cost_usd": UsageLedger.cost_usd + stmt.excluded.cost_usd},
    )
    await db.execute(stmt)


@dataclass(frozen=True)
class BudgetStatus:
    spent: Decimal
    cap: Decimal

    @property
    def exceeded(self) -> bool:
        return self.spent >= self.cap

    @property
    def warn(self) -> bool:
        return self.cap > 0 and self.spent >= self.cap * WARN_FRACTION


async def budget_status(db: AsyncSession, user: User) -> BudgetStatus:
    return BudgetStatus(spent=await month_spend(db, user.id), cap=user.monthly_cap_usd)


async def admin_contact(db: AsyncSession) -> str:
    """Name (or email) of an active admin, for the over-cap message."""
    admin = await db.scalar(
        select(User)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.created_at)
        .limit(1)
    )
    if admin is None:
        return "an admin"
    return admin.name or admin.email

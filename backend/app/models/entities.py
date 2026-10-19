"""SQLAlchemy models for the data model in SPEC section 9.

Additions beyond the spec are marked "ext:" and recorded in docs/TASKS.md (Decisions log).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Price, created_at_col, uuid_pk


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'member')", name="role"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # ext: display name, used in the budget-refusal message ("ask <admin name>").
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="member")
    monthly_cap_usd: Mapped[Decimal]
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = created_at_col()


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), default="New chat")
    current_model: Mapped[str | None] = mapped_column(String(200))
    current_category: Mapped[str | None] = mapped_column(String(50))
    locked_model: Mapped[str | None] = mapped_column(String(200))
    confidential: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), onupdate=func.clock_timestamp()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'tool', 'system')", name="role"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]]
    # ext: the user message this assistant/tool message answers; groups one turn of the agent loop.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    # ext: true once the turn was regenerated; superseded messages stay for routing data
    # but are hidden from the thread and from the model's history.
    superseded: Mapped[bool] = mapped_column(default=False)
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    cached_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None]
    latency_ms: Mapped[int | None]
    created_at: Mapped[datetime] = created_at_col()


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = uuid_pk()
    # ext: owner, so uploads made before a message is sent are still isolated per user.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    s3_key: Mapped[str] = mapped_column(String(500), unique=True)
    filename: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None]
    est_tokens: Mapped[int | None]
    created_at: Mapped[datetime] = created_at_col()


class RoutingDecision(Base):
    __tablename__ = "routing_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    # The assistant message this decision produced.
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(50))
    confidence: Mapped[float | None]
    is_followup: Mapped[bool] = mapped_column(default=False)
    needs: Mapped[list[Any]] = mapped_column(default=list)
    candidates: Mapped[list[Any]] = mapped_column(default=list)
    chosen_model: Mapped[str] = mapped_column(String(200))
    answered_model: Mapped[str | None] = mapped_column(String(200))
    switched: Mapped[bool] = mapped_column(default=False)
    forced: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    classifier_latency_ms: Mapped[int | None]
    overridden_to: Mapped[str | None] = mapped_column(String(200))
    regenerated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = created_at_col()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("rating IN (-1, 1)", name="rating"),
        UniqueConstraint("message_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ToolRun(Base):
    __tablename__ = "tool_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_approval', 'running', 'succeeded', 'failed', 'rejected')",
            name="status",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # The assistant message that requested the tool call.
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    # ext: the model's tool_call id, needed to answer the call after an approval pause.
    tool_call_id: Mapped[str] = mapped_column(String(200), default="")
    tool: Mapped[str] = mapped_column(String(200))
    input: Mapped[dict[str, Any]]
    output_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))


class CatalogModel(Base):
    __tablename__ = "models"

    slug: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    context_length: Mapped[int] = mapped_column(Integer)
    input_modalities: Mapped[list[Any]] = mapped_column(default=list)
    supported_parameters: Mapped[list[Any]] = mapped_column(default=list)
    # USD per token, as OpenRouter reports pricing.
    price_prompt: Mapped[Decimal | None] = mapped_column(Price)
    price_completion: Mapped[Decimal | None] = mapped_column(Price)
    has_zdr_endpoint: Mapped[bool] = mapped_column(default=False)
    available: Mapped[bool] = mapped_column(default=True)
    last_seen_at: Mapped[datetime]


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = created_at_col()

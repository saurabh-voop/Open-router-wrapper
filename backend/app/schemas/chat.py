import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    confidential: bool = False


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    # Explicit null unlocks; omit the field to leave it unchanged.
    locked_model: str | None = None
    confidential: bool | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    current_model: str | None
    current_category: str | None
    locked_model: str | None
    confidential: bool
    created_at: datetime
    updated_at: datetime


class RoutingOut(BaseModel):
    category: str
    reason: str
    switched: bool
    forced: bool
    chosen_model: str
    answered_model: str | None
    overridden_to: str | None


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime: str
    size_bytes: int
    page_count: int | None
    est_tokens: int | None


class ToolRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool: str
    status: str
    input: dict[str, Any]
    output_summary: str | None
    duration_ms: int | None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    parent_id: uuid.UUID | None
    text: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)
    tool_runs: list[ToolRunOut] = Field(default_factory=list)
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cost_usd: Decimal | None
    routing: RoutingOut | None = None
    rating: int | None = None
    error: str | None = None
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    web_search: bool = False
    code_execution: bool = False
    connectors: bool = False


class RegenerateRequest(BaseModel):
    model: str | None = None


class FeedbackRequest(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=5000)

"""Conversations CRUD (SPEC section 12). Every query filters by the current user's id."""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

import app.services.files as file_service
from app.api.deps import CurrentUser, DbSession
from app.models import Attachment, Conversation, Feedback, Message, RoutingDecision, ToolRun
from app.router.config import get_routing_config
from app.schemas.chat import (
    AttachmentOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    RoutingOut,
    ToolRunOut,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def get_owned_conversation(
    db: DbSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conv = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conv is None:
        # 404, not 403: do not reveal that another user's conversation exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


@router.get("", response_model=list[ConversationOut])
async def list_conversations(user: CurrentUser, db: DbSession) -> list[Conversation]:
    rows = await db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(500)
    )
    return list(rows)


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate, user: CurrentUser, db: DbSession
) -> Conversation:
    conv = Conversation(
        user_id=user.id, title=body.title or "New chat", confidential=body.confidential
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> ConversationDetail:
    conv = await get_owned_conversation(db, user.id, conversation_id)
    messages = await thread_messages(db, conv, user.id)
    return ConversationDetail(
        **ConversationOut.model_validate(conv).model_dump(), messages=messages
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: uuid.UUID, body: ConversationUpdate, user: CurrentUser, db: DbSession
) -> Conversation:
    conv = await get_owned_conversation(db, user.id, conversation_id)
    fields = body.model_fields_set
    if body.title is not None:
        conv.title = body.title
    if "locked_model" in fields:
        if body.locked_model is not None and body.locked_model not in set(
            get_routing_config().chat_slugs()
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "That model is not in the allowed list"
            )
        conv.locked_model = body.locked_model
    if body.confidential is not None:
        conv.confidential = body.confidential
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    conv = await get_owned_conversation(db, user.id, conversation_id)
    keys = list(
        await db.scalars(
            select(Attachment.s3_key).where(
                Attachment.conversation_id == conv.id, Attachment.user_id == user.id
            )
        )
    )
    # Delete objects first: a DB row without its object is harmless, the reverse leaks data.
    await file_service.delete_objects(keys)
    await db.delete(conv)
    await db.commit()


# ---------------------------------------------------------------- thread view


def _text(content: dict[str, Any]) -> str:
    value = content.get("text")
    return value if isinstance(value, str) else ""


async def thread_messages(
    db: DbSession, conv: Conversation, user_id: uuid.UUID
) -> list[MessageOut]:
    """User and final assistant messages for display, with routing, tools and attachments."""
    rows = list(
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.superseded.is_(False))
            .order_by(Message.created_at, Message.id)
        )
    )
    ids = [m.id for m in rows]
    if not ids:
        return []

    decisions = {
        d.message_id: d
        for d in await db.scalars(
            select(RoutingDecision).where(RoutingDecision.message_id.in_(ids))
        )
    }
    ratings = {
        f.message_id: f.rating
        for f in await db.scalars(
            select(Feedback).where(Feedback.message_id.in_(ids), Feedback.user_id == user_id)
        )
    }
    attachments: dict[uuid.UUID, list[AttachmentOut]] = {}
    for a in await db.scalars(
        select(Attachment).where(Attachment.message_id.in_(ids), Attachment.user_id == user_id)
    ):
        if a.message_id is not None:
            attachments.setdefault(a.message_id, []).append(AttachmentOut.model_validate(a))
    runs_by_message: dict[uuid.UUID, list[ToolRunOut]] = {}
    for r in await db.scalars(
        select(ToolRun).where(ToolRun.message_id.in_(ids)).order_by(ToolRun.created_at)
    ):
        runs_by_message.setdefault(r.message_id, []).append(ToolRunOut.model_validate(r))

    out: list[MessageOut] = []
    turn_runs: dict[uuid.UUID, list[ToolRunOut]] = {}
    for m in rows:
        if m.role == "user":
            out.append(
                MessageOut(
                    id=m.id,
                    role="user",
                    parent_id=None,
                    text=_text(m.content),
                    attachments=attachments.get(m.id, []),
                    model=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    cached_tokens=None,
                    cost_usd=None,
                    created_at=m.created_at,
                )
            )
            continue
        if m.role != "assistant":
            continue
        key = m.parent_id or m.id
        turn_runs.setdefault(key, []).extend(runs_by_message.get(m.id, []))
        if not m.content.get("final"):
            continue  # intermediate tool-calling step; its tool runs show on the final answer
        d = decisions.get(m.id)
        out.append(
            MessageOut(
                id=m.id,
                role="assistant",
                parent_id=m.parent_id,
                text=_text(m.content),
                citations=list(m.content.get("citations") or []),
                files=list(m.content.get("files") or []),
                tool_runs=turn_runs.pop(key, []),
                model=m.model,
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                cached_tokens=m.cached_tokens,
                cost_usd=m.cost_usd,
                routing=RoutingOut(
                    category=d.category,
                    reason=d.reason,
                    switched=d.switched,
                    forced=d.forced,
                    chosen_model=d.chosen_model,
                    answered_model=d.answered_model,
                    overridden_to=d.overridden_to,
                )
                if d
                else None,
                rating=ratings.get(m.id),
                error=m.content.get("error"),
                created_at=m.created_at,
            )
        )
    return out

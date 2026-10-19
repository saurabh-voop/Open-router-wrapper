"""Sending messages (SSE), regeneration, feedback, tool approvals, model list (SPEC 12)."""

from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.agent.stream import sse_response
from app.agent.tools.registry import ToolProvider
from app.agent.turn import ACTIVE_STATUSES, STATUS_STREAMING, Emit, Turn, TurnOptions
from app.api.conversations import get_owned_conversation
from app.api.deps import CurrentUser, DbSession
from app.core.db import get_sessionmaker
from app.llm.catalog import load_catalog
from app.llm.deps import get_openrouter
from app.models import Attachment, Conversation, Feedback, Message, RoutingDecision, ToolRun
from app.router.classifier import Classification
from app.router.config import get_routing_config
from app.schemas.chat import FeedbackRequest, RegenerateRequest, SendMessageRequest

router = APIRouter(tags=["messages"])

TITLE_CHARS = 60


@lru_cache
def get_tool_provider() -> ToolProvider:
    return ToolProvider()


def _turn(
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_message_id: uuid.UUID,
    final_message_id: uuid.UUID,
    options: TurnOptions,
) -> Turn:
    return Turn(
        sessionmaker=get_sessionmaker(),
        client=get_openrouter(),
        config=get_routing_config(),
        tools=get_tool_provider(),
        user_id=user_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        final_message_id=final_message_id,
        options=options,
    )


async def _ensure_idle(db: DbSession, conv: Conversation) -> None:
    active = await db.scalar(
        select(Message.content["status"].astext)
        .where(
            Message.conversation_id == conv.id,
            Message.role == "assistant",
            Message.superseded.is_(False),
            Message.content["status"].astext.in_(ACTIVE_STATUSES),
        )
        .limit(1)
    )
    if active == "awaiting_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, "Approve or reject the pending action first.")
    if active is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the current answer to finish.")


def _placeholder(conv_id: uuid.UUID, parent_id: uuid.UUID) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        role="assistant",
        parent_id=parent_id,
        content={"final": True, "status": STATUS_STREAMING, "text": ""},
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID, body: SendMessageRequest, user: CurrentUser, db: DbSession
) -> StreamingResponse:
    conv = await get_owned_conversation(db, user.id, conversation_id)
    await _ensure_idle(db, conv)

    attachments: list[Attachment] = []
    if body.attachment_ids:
        attachments = list(
            await db.scalars(
                select(Attachment).where(
                    Attachment.id.in_(body.attachment_ids),
                    Attachment.user_id == user.id,
                    Attachment.message_id.is_(None),
                )
            )
        )
        if len(attachments) != len(set(body.attachment_ids)) or any(
            a.conversation_id not in (None, conv.id) for a in attachments
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
        had_attachments = await db.scalar(
            select(Attachment.id)
            .where(Attachment.conversation_id == conv.id, Attachment.message_id.is_not(None))
            .limit(1)
        )
        if had_attachments is None:
            conv.confidential = True  # default on for conversations with attachments (SPEC 10)

    user_msg = Message(
        id=uuid.uuid4(), conversation_id=conv.id, role="user", content={"text": body.text}
    )
    db.add(user_msg)
    await db.flush()
    for a in attachments:
        a.conversation_id = conv.id
        a.message_id = user_msg.id
    final = _placeholder(conv.id, user_msg.id)
    db.add(final)
    if conv.title == "New chat":
        title = " ".join(body.text.split())[:TITLE_CHARS]
        conv.title = title or "New chat"
    await db.commit()

    toggles = frozenset(
        name
        for name, on in (
            ("web_search", body.web_search),
            ("code_execution", body.code_execution),
            ("connectors", body.connectors),
        )
        if on
    )
    turn = _turn(user.id, conv.id, user_msg.id, final.id, TurnOptions(toggles=toggles))
    return sse_response(turn.start)


async def _owned_message(db: DbSession, user_id: uuid.UUID, message_id: uuid.UUID) -> Message:
    msg = await db.scalar(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.id == message_id, Conversation.user_id == user_id)
    )
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return msg


@router.post("/messages/{message_id}/regenerate")
async def regenerate(
    message_id: uuid.UUID, body: RegenerateRequest, user: CurrentUser, db: DbSession
) -> StreamingResponse:
    old = await _owned_message(db, user.id, message_id)
    if old.role != "assistant" or not old.content.get("final") or old.parent_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only answers can be regenerated")
    if old.superseded:
        raise HTTPException(status.HTTP_409_CONFLICT, "This answer was already replaced")
    conv = await get_owned_conversation(db, user.id, old.conversation_id)
    await _ensure_idle(db, conv)
    latest_user = await db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conv.id,
            Message.role == "user",
            Message.superseded.is_(False),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    if latest_user != old.parent_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only the latest answer can be re-run")
    if body.model is not None and body.model not in get_routing_config().chat_slugs():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That model is not allowed")

    decision = await db.scalar(select(RoutingDecision).where(RoutingDecision.message_id == old.id))
    reuse = None
    if decision is not None:
        decision.regenerated = True
        decision.overridden_to = body.model
        reuse = Classification(
            category=decision.category,
            confidence=decision.confidence if decision.confidence is not None else 0.0,
            is_followup=decision.is_followup,
            needs=list(decision.needs or []),
            reason=decision.reason,
            source="rule",
        )
    await db.execute(
        update(Message)
        .where(Message.parent_id == old.parent_id, Message.conversation_id == conv.id)
        .values(superseded=True)
    )
    final = _placeholder(conv.id, old.parent_id)
    db.add(final)
    await db.commit()

    options = TurnOptions(override_model=body.model, reuse=reuse)
    turn = _turn(user.id, conv.id, old.parent_id, final.id, options)
    return sse_response(turn.start)


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(
    message_id: uuid.UUID, body: FeedbackRequest, user: CurrentUser, db: DbSession
) -> None:
    msg = await _owned_message(db, user.id, message_id)
    if msg.role != "assistant":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only answers can be rated")
    stmt = insert(Feedback).values(
        message_id=msg.id, user_id=user.id, rating=body.rating, comment=body.comment
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Feedback.message_id, Feedback.user_id],
        set_={"rating": stmt.excluded.rating, "comment": stmt.excluded.comment},
    )
    await db.execute(stmt)
    await db.commit()


async def _decide_tool_run(
    tool_run_id: uuid.UUID, approved: bool, user: CurrentUser, db: DbSession
) -> StreamingResponse:
    row = (
        await db.execute(
            select(ToolRun, Message)
            .join(Message, Message.id == ToolRun.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(ToolRun.id == tool_run_id, Conversation.user_id == user.id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    run, step = row
    if run.status != "pending_approval" or step.parent_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That action is no longer pending")
    final = await db.scalar(
        select(Message).where(
            Message.parent_id == step.parent_id,
            Message.superseded.is_(False),
            Message.content["final"].as_boolean().is_(True),
        )
    )
    if final is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That action is no longer pending")
    turn = _turn(user.id, step.conversation_id, step.parent_id, final.id, TurnOptions())

    async def run_resume(emit: Emit) -> None:
        await turn.resume(emit, tool_run_id=run.id, approved=approved)

    return sse_response(run_resume)


@router.post("/tool-runs/{tool_run_id}/approve")
async def approve_tool_run(
    tool_run_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> StreamingResponse:
    return await _decide_tool_run(tool_run_id, True, user, db)


@router.post("/tool-runs/{tool_run_id}/reject")
async def reject_tool_run(
    tool_run_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> StreamingResponse:
    return await _decide_tool_run(tool_run_id, False, user, db)


@router.get("/models")
async def list_models(user: CurrentUser, db: DbSession) -> list[dict[str, object]]:
    """Allowed models with capabilities, for the "Re-run with..." menu."""
    catalog = await load_catalog(db)
    out: list[dict[str, object]] = []
    for slug in get_routing_config().chat_slugs():
        m = catalog.get(slug)
        if m is None or not m.available or not m.has_zdr_endpoint:
            continue
        out.append(
            {
                "slug": m.slug,
                "name": m.name,
                "context_length": m.context_length,
                "vision": "image" in (m.input_modalities or []),
                "file_input": "file" in (m.input_modalities or []),
                "tools": "tools" in (m.supported_parameters or []),
                "price_prompt": str(m.price_prompt) if m.price_prompt is not None else None,
                "price_completion": str(m.price_completion)
                if m.price_completion is not None
                else None,
            }
        )
    return out

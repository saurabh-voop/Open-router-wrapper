"""Build the model's message list from the stored conversation."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.files as file_service
from app.models import Attachment, Conversation, Message
from app.router.decision import AttachmentInfo
from app.services import attachments as att_service

CHARS_PER_TOKEN = 4
SYSTEM_PROMPT_TOKENS = 400


def system_prompt(now: datetime | None = None) -> str:
    today = (now or datetime.now(UTC)).strftime("%A %d %B %Y")
    return (
        "You are Relay, an assistant for a small industrial sales team in India. "
        f"Today is {today}. Answer accurately and concisely; use Markdown for structure, "
        "tables and code. If you are unsure or lack information, say so instead of guessing. "
        "When you use web sources, cite them."
    )


@dataclass
class History:
    messages: list[dict[str, Any]]
    classifier_turns: list[dict[str, str]]  # recent user/assistant text, for the classifier
    est_tokens: int
    current_attachments: list[AttachmentInfo] = field(default_factory=list)
    attachment_rows: list[Attachment] = field(default_factory=list)


def _estimate(parts: Any) -> int:
    if isinstance(parts, str):
        return len(parts) // CHARS_PER_TOKEN
    if isinstance(parts, list):
        total = 0
        for p in parts:
            if p.get("type") == "text":
                total += len(p.get("text", "")) // CHARS_PER_TOKEN
        return total
    return 0


def _complete(turn_steps: list[Message]) -> bool:
    """True if every tool call in these steps has a tool result."""
    called = {c["id"] for s in turn_steps for c in s.content.get("tool_calls") or []}
    answered = {s.content.get("tool_call_id") for s in turn_steps if s.role == "tool"}
    return called <= answered


async def build_history(
    db: AsyncSession,
    conv: Conversation,
    *,
    current_user_message_id: uuid.UUID,
    current_text: str | None = None,
    sandbox_available: bool = False,
) -> History:
    """Messages up to and including the current turn, oldest first.

    ``current_text`` replaces the current user message text (e.g. with a slash command
    removed). The current turn's placeholder final message is skipped.
    """
    rows = list(
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.superseded.is_(False))
            .order_by(Message.created_at, Message.id)
        )
    )
    attachments: dict[uuid.UUID, list[Attachment]] = {}
    for a in await db.scalars(
        select(Attachment)
        .where(Attachment.conversation_id == conv.id, Attachment.user_id == conv.user_id)
        .order_by(Attachment.created_at)
    ):
        if a.message_id is not None:
            attachments.setdefault(a.message_id, []).append(a)

    users = [m for m in rows if m.role == "user"]
    steps: dict[uuid.UUID, list[Message]] = {}
    finals: dict[uuid.UUID, Message] = {}
    for m in rows:
        if m.role == "user" or m.parent_id is None:
            continue
        if m.role == "assistant" and m.content.get("final"):
            finals[m.parent_id] = m
        else:
            steps.setdefault(m.parent_id, []).append(m)

    history = History(
        messages=[{"role": "system", "content": system_prompt()}],
        classifier_turns=[],
        est_tokens=SYSTEM_PROMPT_TOKENS,
    )
    for user_msg in users:
        is_current = user_msg.id == current_user_message_id
        text = (
            current_text
            if (is_current and current_text is not None)
            else user_msg.content.get("text", "")
        )
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for att in attachments.get(user_msg.id, []):
            data = await file_service.get_object(att.s3_key)
            try:
                parts += await asyncio.to_thread(
                    att_service.content_parts, att, data, sandbox_available=sandbox_available
                )
            except att_service.AttachmentError as err:
                parts.append({"type": "text", "text": f"[{err}]"})
            history.est_tokens += att.est_tokens or 0
            if is_current:
                history.current_attachments.append(att_service.info(att))
                history.attachment_rows.append(att)
        history.est_tokens += _estimate(parts)
        history.messages.append({"role": "user", "content": parts if len(parts) > 1 else text})
        if not is_current:
            history.classifier_turns.append({"role": "user", "text": text})

        turn_steps = steps.get(user_msg.id, [])
        if not is_current and not _complete(turn_steps):
            turn_steps = []  # interrupted tool loop: unanswered tool calls would be rejected
        for step in turn_steps:
            if step.role == "assistant":
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": step.content.get("text") or "",
                }
                calls = step.content.get("tool_calls") or []
                if calls:
                    msg["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in calls
                    ]
                history.messages.append(msg)
                history.est_tokens += _estimate(msg["content"])
            elif step.role == "tool":
                content = str(step.content.get("content") or "")
                history.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": step.content.get("tool_call_id", ""),
                        "content": content,
                    }
                )
                history.est_tokens += _estimate(content)

        final = finals.get(user_msg.id)
        if is_current or final is None:
            continue
        answer = final.content.get("text") or ""
        if answer and not final.content.get("error"):
            history.messages.append({"role": "assistant", "content": answer})
            history.est_tokens += _estimate(answer)
            history.classifier_turns.append({"role": "assistant", "text": answer})
    return history

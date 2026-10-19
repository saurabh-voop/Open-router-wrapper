"""One chat turn: budget check -> route -> agent loop -> persist (SPEC sections 5, 8, 11).

A turn owns one placeholder "final" assistant message, created by the API before the turn
starts. The routing decision links to it. Intermediate tool-calling steps are separate
assistant messages with the same ``parent_id`` (the user message).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.history import History, build_history
from app.agent.tools.base import OutputFile, ToolContext, ToolResult, Toolset
from app.agent.tools.registry import ToolProvider
from app.agent.tools.web_search import citations_from_annotations
from app.llm.catalog import load_catalog
from app.llm.openrouter import Completion, OpenRouterClient, OpenRouterError, TextDelta, ToolCall
from app.models import Conversation, Message, RoutingDecision, ToolRun, User
from app.router.classifier import Classification
from app.router.config import RoutingConfig
from app.router.decision import RouteRequest, RoutingError, route
from app.router.sticky import StickyState
from app.services import budget

logger = logging.getLogger(__name__)

Event = dict[str, Any]
Emit = Callable[[Event], Awaitable[None]]

STATUS_STREAMING = "streaming"
STATUS_AWAITING = "awaiting_approval"
STATUS_DONE = "done"
STATUS_ERROR = "error"
ACTIVE_STATUSES = (STATUS_STREAMING, STATUS_AWAITING)
TOOL_OUTPUT_SUMMARY_CHARS = 300


@dataclass(frozen=True)
class TurnOptions:
    toggles: frozenset[str] = frozenset()
    override_model: str | None = None
    reuse: Classification | None = None


@dataclass
class _LoopState:
    model: str
    fallbacks: list[str]
    tool_names: list[str]
    step: int = 0
    turn_cost: Decimal = Decimal(0)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    display_parts: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)


class BudgetExceeded(Exception):
    pass


class Turn:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: OpenRouterClient,
        config: RoutingConfig,
        tools: ToolProvider,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_message_id: uuid.UUID,
        final_message_id: uuid.UUID,
        options: TurnOptions = TurnOptions(),
    ) -> None:
        self.sessionmaker = sessionmaker
        self.client = client
        self.config = config
        self.tools = tools
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.user_message_id = user_message_id
        self.final_message_id = final_message_id
        self.options = options

    # ------------------------------------------------------------ entry points

    async def start(self, emit: Emit) -> None:
        async with self.sessionmaker() as db:
            user, conv, final = await self._load(db)
            try:
                await self._start(db, user, conv, final, emit)
            except Exception:
                # Never leave the placeholder "streaming": that would lock the conversation.
                logger.exception("turn failed before the model loop")
                await self._fail(db, final, "Something went wrong while answering.", emit)

    async def _start(
        self, db: AsyncSession, user: User, conv: Conversation, final: Message, emit: Emit
    ) -> None:
        try:
            await self._check_budget(db, user)
        except BudgetExceeded as err:
            await self._fail(db, final, str(err), emit)
            return

        user_msg = await db.get(Message, self.user_message_id)
        assert user_msg is not None
        history = await build_history(
            db,
            conv,
            current_user_message_id=user_msg.id,
            sandbox_available=self.tools.sandbox_available,
        )
        catalog = await load_catalog(db)
        request = RouteRequest(
            text=str(user_msg.content.get("text") or ""),
            history=history.classifier_turns,
            attachments=history.current_attachments,
            est_tokens=history.est_tokens,
            state=StickyState(conv.current_model, conv.current_category, conv.locked_model),
            confidential=conv.confidential,
            toggles=self.options.toggles,
            override_model=self.options.override_model,
            reuse=self.options.reuse,
        )
        try:
            outcome = await route(request, client=self.client, config=self.config, catalog=catalog)
        except RoutingError as err:
            await self._fail(db, final, err.user_message, emit)
            return

        if outcome.text != request.text:
            _replace_current_user_text(history, outcome.text)
        if outcome.classifier_cost:
            await budget.add_cost(db, user.id, outcome.classifier_cost)

        decision = RoutingDecision(
            message_id=final.id,
            category=outcome.category,
            confidence=outcome.confidence,
            is_followup=outcome.is_followup,
            needs=outcome.needs,
            candidates=outcome.candidates,
            chosen_model=outcome.model,
            switched=outcome.switched,
            forced=outcome.forced,
            reason=outcome.reason,
            classifier_latency_ms=outcome.classifier_latency_ms,
        )
        db.add(decision)
        conv.current_model = outcome.model
        conv.current_category = outcome.new_category
        final.content = {
            **final.content,
            "tools": sorted(outcome.tools),
            "fallbacks": outcome.fallbacks,
        }
        await db.commit()

        await emit(
            {
                "type": "route",
                "message_id": str(final.id),
                "model": outcome.model,
                "category": outcome.category,
                "reason": outcome.reason,
                "switched": outcome.switched,
                "forced": outcome.forced,
                "confidential": conv.confidential,
            }
        )
        state = _LoopState(
            model=outcome.model, fallbacks=outcome.fallbacks, tool_names=sorted(outcome.tools)
        )
        if outcome.classifier_cost:
            state.turn_cost += outcome.classifier_cost
        await self._loop(db, user, conv, final, decision, history.messages, state, emit)

    async def resume(self, emit: Emit, *, tool_run_id: uuid.UUID, approved: bool) -> None:
        """Continue a turn paused for approval of a connector write action (SPEC 8.4)."""
        async with self.sessionmaker() as db:
            user, conv, final = await self._load(db)
            # Claim the run atomically: two Approve clicks must not run the action twice.
            claimed = await db.scalar(
                update(ToolRun)
                .where(ToolRun.id == tool_run_id, ToolRun.status == "pending_approval")
                .values(
                    status="running" if approved else "rejected",
                    approved_by=user.id if approved else None,
                )
                .returning(ToolRun.id)
            )
            if claimed is None:
                await db.rollback()
                await emit({"type": "error", "message": "That action is no longer pending."})
                await emit({"type": "done", "message_id": str(final.id)})
                return
            final.content = {**final.content, "status": STATUS_STREAMING}
            await db.commit()
            try:
                await self._resume(db, user, conv, final, tool_run_id, approved, emit)
            except Exception:
                logger.exception("turn failed while resuming")
                await self._fail(db, final, "Something went wrong while answering.", emit)

    async def _resume(
        self,
        db: AsyncSession,
        user: User,
        conv: Conversation,
        final: Message,
        tool_run_id: uuid.UUID,
        approved: bool,
        emit: Emit,
    ) -> None:
        run = await db.get(ToolRun, tool_run_id, populate_existing=True)
        decision = await db.scalar(
            select(RoutingDecision).where(RoutingDecision.message_id == final.id)
        )
        if run is None or decision is None:
            raise LookupError("paused turn is incomplete")
        steps = await self._turn_steps(db)
        state = _LoopState(
            model=decision.answered_model or decision.chosen_model,
            fallbacks=[],
            tool_names=list(final.content.get("tools") or []),
            step=sum(1 for s in steps if s.role == "assistant"),
            display_parts=list(final.content.get("display_parts") or []),
            files=list(final.content.get("files") or []),
        )
        toolset = await self.tools.build(state.tool_names, user=user, conversation=conv)
        step_msg = await db.get(Message, run.message_id)
        assert step_msg is not None
        ctx = ToolContext(user_id=user.id, conversation_id=conv.id, message_id=step_msg.id)
        if not approved:
            result = ToolResult(
                content="The user rejected this action. It was not performed.",
                summary="rejected by user",
                ok=False,
            )
            await emit({"type": "tool_end", "tool_run_id": str(run.id), "tool": run.tool,
                        "ok": False, "summary": "Rejected"})  # fmt: skip
        elif run.tool not in toolset.local:
            run.status = "failed"
            result = ToolResult(
                f"The {run.tool} action is unavailable right now (connector unreachable or "
                "disconnected). It was not performed.",
                "tool unavailable",
                ok=False,
            )
            await emit({"type": "tool_end", "tool_run_id": str(run.id), "tool": run.tool,
                        "ok": False, "summary": "Unavailable"})  # fmt: skip
        else:
            result = await self._execute(db, toolset, run, ctx, emit)
        await self._store_tool_message(db, run, result, state)
        await db.commit()

        calls = [ToolCall(**c) for c in step_msg.content.get("tool_calls") or []]
        answered = {s.content.get("tool_call_id") for s in steps if s.role == "tool"}
        answered.add(run.tool_call_id)
        remaining = [c for c in calls if c.id not in answered]
        if await self._run_calls(db, toolset, remaining, ctx, final, state, emit) == "paused":
            return
        try:
            await self._check_budget(db, user)
        except BudgetExceeded as err:
            await self._fail(db, final, str(err), emit, state=state)
            return

        history = await build_history(
            db,
            conv,
            current_user_message_id=self.user_message_id,
            sandbox_available=self.tools.sandbox_available,
        )
        await self._loop(db, user, conv, final, decision, history.messages, state, emit)

    # ------------------------------------------------------------ the loop

    async def _loop(
        self,
        db: AsyncSession,
        user: User,
        conv: Conversation,
        final: Message,
        decision: RoutingDecision,
        messages: list[dict[str, Any]],
        state: _LoopState,
        emit: Emit,
    ) -> None:
        toolset = await self.tools.build(state.tool_names, user=user, conversation=conv)
        max_steps = self.config.max_tool_steps
        try:
            while True:
                last_step = state.step >= max_steps - 1
                specs = toolset.specs() if toolset else None
                # On the last allowed step the model must answer instead of calling tools.
                extra = {"tool_choice": "none"} if (specs and last_step) else None
                completion = await self._call_model(messages, state, specs, extra, emit)
                state.step += 1
                # The loop stays on the model that answered: no switching mid-loop (SPEC 5.7).
                state.model = completion.model
                state.fallbacks = []
                decision.answered_model = completion.model
                await self._record_cost(db, user, completion, state)

                if completion.tool_calls and specs and not last_step:
                    step_msg = Message(
                        conversation_id=conv.id,
                        role="assistant",
                        parent_id=self.user_message_id,
                        content={
                            "final": False,
                            "text": completion.text,
                            "tool_calls": [
                                {"id": c.id, "name": c.name, "arguments": c.arguments}
                                for c in completion.tool_calls
                            ],
                        },
                        **_usage_fields(completion),
                    )
                    db.add(step_msg)
                    await db.flush()
                    messages.append(
                        {
                            "role": "assistant",
                            "content": completion.text or "",
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {"name": c.name, "arguments": c.arguments},
                                }
                                for c in completion.tool_calls
                            ],
                        }
                    )
                    ctx = ToolContext(
                        user_id=user.id, conversation_id=conv.id, message_id=step_msg.id
                    )
                    outcome = await self._run_calls(
                        db, toolset, completion.tool_calls, ctx, final, state, emit, messages
                    )
                    if outcome == "paused":
                        return
                    await self._check_budget(db, user)
                    continue

                await self._finish(db, user, final, completion, state, emit)
                return
        except OpenRouterError as err:
            if err.usage is not None and err.usage.cost:
                # A failed stream can still be charged; keep the ledger honest.
                state.turn_cost += err.usage.cost
                await budget.add_cost(db, user.id, err.usage.cost)
                await db.commit()
            await self._fail(db, final, err.user_message, emit, state=state)
        except BudgetExceeded as err:
            await self._fail(db, final, str(err), emit, state=state)
        except Exception:
            logger.exception("turn failed")
            await self._fail(db, final, "Something went wrong while answering.", emit, state=state)

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        state: _LoopState,
        specs: list[dict[str, Any]] | None,
        extra: dict[str, Any] | None,
        emit: Emit,
    ) -> Completion:
        completion: Completion | None = None
        async for event in self.client.stream_chat(
            model=state.model,
            messages=messages,
            fallbacks=state.fallbacks,
            tools=specs,
            extra=extra,
        ):
            if isinstance(event, TextDelta):
                await emit({"type": "token", "text": event.text})
            else:
                completion = event
        assert completion is not None
        if completion.text:
            state.display_parts.append(completion.text)
        return completion

    async def _run_calls(
        self,
        db: AsyncSession,
        toolset: Toolset,
        calls: list[ToolCall],
        ctx: ToolContext,
        final: Message,
        state: _LoopState,
        emit: Emit,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        for call in calls:
            args = call.parsed_arguments()
            tool = toolset.local.get(call.name)
            run = ToolRun(
                message_id=ctx.message_id,
                tool_call_id=call.id,
                tool=call.name,
                input=args,
                status="running",
            )
            db.add(run)
            await db.flush()
            if tool is not None and tool.writes:
                run.status = "pending_approval"
                final.content = {
                    **final.content,
                    "status": STATUS_AWAITING,
                    "display_parts": state.display_parts,
                    "files": state.files,
                }
                await db.commit()
                await emit(
                    {
                        "type": "approval_required",
                        "tool_run_id": str(run.id),
                        "tool": call.name,
                        "input": args,
                    }
                )
                await emit({"type": "done", "message_id": str(final.id), "paused": True})
                return "paused"
            if tool is None:
                run.status = "failed"
                result = ToolResult(f"Unknown tool: {call.name}", "unknown tool", ok=False)
            else:
                result = await self._execute(db, toolset, run, ctx, emit)
            tool_msg = await self._store_tool_message(db, run, result, state)
            if messages is not None:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_msg.content["content"],
                    }
                )
        await db.commit()
        return "continued"

    async def _execute(
        self, db: AsyncSession, toolset: Toolset, run: ToolRun, ctx: ToolContext, emit: Emit
    ) -> ToolResult:
        tool = toolset.local[run.tool]
        await emit(
            {"type": "tool_start", "tool_run_id": str(run.id), "tool": run.tool, "input": run.input}
        )
        started = time.monotonic()
        try:
            result = await tool.run(run.input, ctx)
        except Exception:
            logger.exception("tool %s failed", run.tool)
            result = ToolResult(
                "The tool failed with an internal error.", "internal error", ok=False
            )
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.status = "succeeded" if result.ok else "failed"
        run.output_summary = result.summary[:TOOL_OUTPUT_SUMMARY_CHARS]
        for f in result.files:
            await emit(
                {
                    "type": "file",
                    "attachment_id": str(f.attachment_id),
                    "filename": f.filename,
                    "mime": f.mime,
                }
            )
        await emit(
            {
                "type": "tool_end",
                "tool_run_id": str(run.id),
                "tool": run.tool,
                "ok": result.ok,
                "summary": result.summary[:TOOL_OUTPUT_SUMMARY_CHARS],
                "duration_ms": run.duration_ms,
            }
        )
        return result

    async def _store_tool_message(
        self, db: AsyncSession, run: ToolRun, result: ToolResult, state: _LoopState
    ) -> Message:
        state.files += [_file_dict(f) for f in result.files]
        msg = Message(
            conversation_id=self.conversation_id,
            role="tool",
            parent_id=self.user_message_id,
            content={"tool_call_id": run.tool_call_id, "name": run.tool, "content": result.content},
        )
        db.add(msg)
        await db.flush()
        return msg

    # ------------------------------------------------------------ bookkeeping

    async def _load(self, db: AsyncSession) -> tuple[User, Conversation, Message]:
        user = await db.get(User, self.user_id)
        conv = await db.scalar(
            select(Conversation).where(
                Conversation.id == self.conversation_id, Conversation.user_id == self.user_id
            )
        )
        final = await db.get(Message, self.final_message_id)
        if user is None or conv is None or final is None or final.conversation_id != conv.id:
            raise LookupError("turn target not found")
        return user, conv, final

    async def _turn_steps(self, db: AsyncSession) -> list[Message]:
        rows = await db.scalars(
            select(Message)
            .where(
                Message.parent_id == self.user_message_id,
                Message.id != self.final_message_id,
                Message.superseded.is_(False),
            )
            .order_by(Message.created_at, Message.id)
        )
        return list(rows)

    async def _check_budget(self, db: AsyncSession, user: User) -> None:
        status = await budget.budget_status(db, user)
        if status.exceeded:
            admin = await budget.admin_contact(db)
            raise BudgetExceeded(
                f"You have reached your monthly budget of ${status.cap:.2f} "
                f"(spent ${status.spent:.2f} this month). Ask {admin} to raise it."
            )

    async def _record_cost(
        self, db: AsyncSession, user: User, completion: Completion, state: _LoopState
    ) -> None:
        usage = completion.usage
        if usage is None:
            return
        state.prompt_tokens += usage.prompt_tokens
        state.completion_tokens += usage.completion_tokens
        if usage.cost is not None:
            state.turn_cost += usage.cost
            await budget.add_cost(db, user.id, usage.cost)
        await db.commit()

    async def _finish(
        self,
        db: AsyncSession,
        user: User,
        final: Message,
        completion: Completion,
        state: _LoopState,
        emit: Emit,
    ) -> None:
        citations = citations_from_annotations(completion.annotations)
        for cite in citations:
            await emit({"type": "citation", **cite})
        final.content = {
            **final.content,
            "final": True,
            "status": STATUS_DONE,
            "text": completion.text,
            "display_parts": state.display_parts,
            "citations": citations,
            "files": state.files,
        }
        for k, v in _usage_fields(completion).items():
            setattr(final, k, v)
        await db.commit()
        await self._emit_usage(db, user, state, emit)
        await emit({"type": "done", "message_id": str(final.id)})

    async def _fail(
        self,
        db: AsyncSession,
        final: Message,
        message: str,
        emit: Emit,
        *,
        state: _LoopState | None = None,
    ) -> None:
        # Discard any half-written step; objects expire on rollback, so reload by id.
        await db.rollback()
        reloaded = await db.get(Message, self.final_message_id, populate_existing=True)
        if reloaded is None:
            raise LookupError("final message disappeared")
        final = reloaded
        partial = "\n\n".join(state.display_parts) if state else ""
        final.content = {
            **final.content,
            "final": True,
            "status": STATUS_ERROR,
            "text": partial,
            "display_parts": state.display_parts if state else [],
            "files": state.files if state else [],
            "error": message,
        }
        if state is not None:
            final.model = state.model
        await db.commit()
        await emit({"type": "error", "message": message, "message_id": str(final.id)})
        if state is not None:
            user = await db.get(User, self.user_id)
            if user is not None:
                await self._emit_usage(db, user, state, emit)
        await emit({"type": "done", "message_id": str(final.id)})

    async def _emit_usage(
        self, db: AsyncSession, user: User, state: _LoopState, emit: Emit
    ) -> None:
        status = await budget.budget_status(db, user)
        await emit(
            {
                "type": "usage",
                "cost_usd": str(state.turn_cost),
                "prompt_tokens": state.prompt_tokens,
                "completion_tokens": state.completion_tokens,
                "month_spend_usd": str(status.spent),
                "monthly_cap_usd": str(status.cap),
                "budget_warning": status.warn,
                "budget_exceeded": status.exceeded,
            }
        )


def _usage_fields(completion: Completion) -> dict[str, Any]:
    usage = completion.usage
    return {
        "model": completion.model,
        "prompt_tokens": usage.prompt_tokens if usage else None,
        "completion_tokens": usage.completion_tokens if usage else None,
        "cached_tokens": usage.cached_tokens if usage else None,
        "cost_usd": usage.cost if usage else None,
        "latency_ms": completion.latency_ms,
    }


def _file_dict(f: OutputFile) -> dict[str, Any]:
    return {"attachment_id": str(f.attachment_id), "filename": f.filename, "mime": f.mime}


def _replace_current_user_text(history: History, text: str) -> None:
    for msg in reversed(history.messages):
        if msg["role"] != "user":
            continue
        if isinstance(msg["content"], str):
            msg["content"] = text
        else:
            msg["content"][0] = {"type": "text", "text": text}
        return

"""Runs a turn in a background task and relays its events as Server-Sent Events.

The task keeps running if the browser disconnects, so the answer is still stored and shows
up after a reload.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi.responses import StreamingResponse

from app.agent.turn import Emit, Event

logger = logging.getLogger(__name__)

_running: set[asyncio.Task[None]] = set()


def encode(event: Event) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


def start_stream(run: Callable[[Emit], Awaitable[None]]) -> AsyncIterator[str]:
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def emit(event: Event) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            await run(emit)
        except Exception:
            logger.exception("turn crashed")
            await queue.put({"type": "error", "message": "Something went wrong while answering."})
            await queue.put({"type": "done"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())
    _running.add(task)
    task.add_done_callback(_running.discard)

    async def events() -> AsyncIterator[str]:
        while (event := await queue.get()) is not None:
            yield encode(event)

    return events()


def sse_response(run: Callable[[Emit], Awaitable[None]]) -> StreamingResponse:
    return StreamingResponse(
        start_stream(run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def wait_for_running_turns() -> None:
    """Test helper and shutdown hook: wait for background turns to finish."""
    if _running:
        await asyncio.gather(*list(_running), return_exceptions=True)

"""Tool interfaces for the agent loop (SPEC section 8)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class OutputFile:
    """A file produced by a tool (e.g. a chart from run_python), stored in S3."""

    attachment_id: uuid.UUID
    filename: str
    mime: str


@dataclass
class ToolResult:
    content: str  # what the model sees
    summary: str  # what is stored in tool_runs.output_summary (no client data at info level)
    ok: bool = True
    files: list[OutputFile] = field(default_factory=list)


@dataclass
class ToolContext:
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID  # the assistant step that requested the call


class LocalTool(Protocol):
    """A tool Relay runs itself (as opposed to OpenRouter server tools like web search)."""

    name: str
    writes: bool  # write tools pause for user approval (SPEC 8.4)

    def spec(self) -> dict[str, Any]: ...

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


@dataclass
class Toolset:
    local: dict[str, LocalTool] = field(default_factory=dict)
    server: list[dict[str, Any]] = field(default_factory=list)  # OpenRouter server tools

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.local.values()] + list(self.server)

    def __bool__(self) -> bool:
        return bool(self.local or self.server)

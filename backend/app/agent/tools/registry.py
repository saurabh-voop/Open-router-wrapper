"""Builds the toolset for a turn from the router's tool names."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from app.agent.tools.base import LocalTool, Toolset
from app.agent.tools.web_search import web_search_tool
from app.models import Conversation, User

ToolFactory = Callable[[User, Conversation], Awaitable[list[LocalTool]]]


class ToolProvider:
    """Maps router tool names to concrete tools.

    ``web_search`` is an OpenRouter server tool. ``run_python`` and ``connectors`` are local
    tools registered by their modules (Phases 6 and 8); unregistered names are skipped.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ToolFactory] = {}

    def register(self, name: str, factory: ToolFactory) -> None:
        self._factories[name] = factory

    @property
    def sandbox_available(self) -> bool:
        return "run_python" in self._factories

    async def build(
        self, names: Iterable[str], *, user: User, conversation: Conversation
    ) -> Toolset:
        toolset = Toolset()
        for name in names:
            if name == "web_search":
                if not conversation.confidential:  # belt and braces; the router already drops it
                    toolset.server.append(web_search_tool())
                continue
            factory = self._factories.get(name)
            if factory is None:
                continue
            for tool in await factory(user, conversation):
                toolset.local[tool.name] = tool
        return toolset

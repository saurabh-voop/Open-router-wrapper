"""Step 3 of routing: when to switch models within a conversation (SPEC section 5.4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["locked", "new", "forced_switch", "keep", "switch"]


@dataclass(frozen=True)
class StickyState:
    current_model: str | None
    current_category: str | None
    locked_model: str | None


def decide(
    state: StickyState,
    *,
    category: str,
    confidence: float,
    is_followup: bool,
    current_passes_filter: bool,
    policy_primary: str | None,
    switch_threshold: float,
) -> Action:
    """The SPEC 5.4 decision tree, verbatim. Pure: no I/O, easy to test."""
    if state.locked_model:
        return "locked"
    if state.current_model is None:
        return "new"
    if not current_passes_filter:
        return "forced_switch"
    if is_followup or category == state.current_category:
        return "keep"
    if confidence >= switch_threshold and policy_primary != state.current_model:
        return "switch"
    return "keep"

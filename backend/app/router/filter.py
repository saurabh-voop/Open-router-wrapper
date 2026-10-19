"""Step 1 of routing: the hard capability filter (SPEC section 5.2).

Drops every model that cannot serve the request. The privacy constraint (a ZDR endpoint)
is never relaxed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.models import CatalogModel


@dataclass(frozen=True)
class Requirements:
    vision: bool = False
    pdf: bool = False  # soft: native file input preferred, file-parser plugin otherwise
    tools: bool = False
    est_tokens: int = 0
    context_safety_factor: float = 1.2

    @property
    def min_context(self) -> int:
        return int(self.est_tokens * self.context_safety_factor)

    def describe(self) -> str:
        """Human-readable, e.g. 'a zero-data-retention model with vision and 400k context'."""
        parts = []
        if self.vision:
            parts.append("vision")
        if self.tools:
            parts.append("tool calling")
        if self.min_context > 0:
            parts.append(f"{_k(self.min_context)} context")
        text = "a zero-data-retention model"
        if parts:
            text += " with " + _join(parts)
        return text


def _k(n: int) -> str:
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def rejection_reason(model: CatalogModel | None, req: Requirements) -> str | None:
    """Why ``model`` fails the hard filter, or None if it passes. Allowlist is checked by caller."""
    if model is None or not model.available:
        return "not in the current OpenRouter catalog"
    if not model.has_zdr_endpoint:
        return "no zero-data-retention endpoint"
    if req.vision and "image" not in (model.input_modalities or []):
        return "no image input"
    if req.tools and "tools" not in (model.supported_parameters or []):
        return "no tool calling"
    if req.min_context and model.context_length < req.min_context:
        return f"context {_k(model.context_length)} < {_k(req.min_context)} needed"
    return None


def has_native_file_input(model: CatalogModel | None) -> bool:
    return model is not None and "file" in (model.input_modalities or [])


@dataclass
class FilterResult:
    candidates: list[str]  # allowlisted slugs that pass, in allowlist order
    rejected: dict[str, str] = field(default_factory=dict)

    def passes(self, slug: str | None) -> bool:
        return slug is not None and slug in self.candidates


def apply_filter(
    allowlist: Iterable[str], catalog: Mapping[str, CatalogModel], req: Requirements
) -> FilterResult:
    result = FilterResult(candidates=[])
    for slug in allowlist:
        reason = rejection_reason(catalog.get(slug), req)
        if reason is None:
            result.candidates.append(slug)
        else:
            result.rejected[slug] = reason
    return result

"""Step 4 of routing: policy lookup (SPEC section 5.5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.llm.openrouter import MAX_FALLBACKS
from app.models import CatalogModel
from app.router.config import RoutingConfig
from app.router.filter import FilterResult, has_native_file_input


@dataclass(frozen=True)
class PolicyChoice:
    primary: str
    fallbacks: list[str]
    # True when none of the category's models passed and another allowed model was used.
    outside_category: bool = False


def surviving(config: RoutingConfig, category: str, flt: FilterResult) -> list[str]:
    return [m for m in config.categories[category].models if flt.passes(m)]


def pick(
    config: RoutingConfig,
    category: str,
    flt: FilterResult,
    catalog: Mapping[str, CatalogModel],
    *,
    prefer_native_file: bool = False,
) -> PolicyChoice | None:
    """First surviving model of the category is primary; the next two are fallbacks.

    If none of the category's models survive, fall back to the default category's list and
    then to any allowed candidate, so an attachment does not dead-end a category whose
    models lack, say, vision. Returns None only when nothing survives at all.
    """
    ordered = surviving(config, category, flt)
    outside = False
    if not ordered:
        outside = True
        ordered = surviving(config, config.default_category, flt) or list(flt.candidates)
    if not ordered:
        return None
    if prefer_native_file:
        native = [m for m in ordered if has_native_file_input(catalog.get(m))]
        ordered = native + [m for m in ordered if m not in native]
    return PolicyChoice(
        primary=ordered[0], fallbacks=ordered[1 : 1 + MAX_FALLBACKS], outside_category=outside
    )


def fallbacks_for(model: str, config: RoutingConfig, category: str, flt: FilterResult) -> list[str]:
    """Fallbacks when keeping ``model``: the category's other surviving models."""
    return [m for m in surviving(config, category, flt) if m != model][:MAX_FALLBACKS]

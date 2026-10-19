"""Typed loader for config/routing.yaml (SPEC section 5)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import get_settings

PLACEHOLDER_PREFIX = "<FILL"

ToolName = Literal["run_python", "web_search", "connectors"]

CATEGORIES = (
    "coding",
    "data_analysis",
    "research",
    "document_qa",
    "writing",
    "reasoning",
    "actions",
    "general",
)


def is_placeholder(slug: str) -> bool:
    return slug.strip().startswith(PLACEHOLDER_PREFIX)


class CategoryPolicy(BaseModel):
    description: str
    examples: list[str] = Field(default_factory=list)
    models: list[str] = Field(min_length=1)
    tools: list[ToolName] = Field(default_factory=list)


class RoutingConfig(BaseModel):
    version: int = 1
    classifier_model: str
    classifier_timeout_s: float = 3.0
    switch_threshold: float = Field(0.75, ge=0, le=1)
    max_tool_steps: int = Field(10, ge=1)
    context_safety_factor: float = Field(1.2, ge=1)
    default_category: str = "general"
    categories: dict[str, CategoryPolicy]
    baseline_model: str
    judge_model: str | None = None

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, v: dict[str, CategoryPolicy]) -> dict[str, CategoryPolicy]:
        unknown = set(v) - set(CATEGORIES)
        if unknown:
            raise ValueError(f"unknown categories in routing.yaml: {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _default_exists(self) -> RoutingConfig:
        if self.default_category not in self.categories:
            raise ValueError(f"default_category {self.default_category!r} is not a category")
        return self

    def all_slugs(self) -> set[str]:
        """Every model slug the config allows (the admin allowlist, SPEC 5.2)."""
        slugs = {self.classifier_model, self.baseline_model}
        if self.judge_model:
            slugs.add(self.judge_model)
        for policy in self.categories.values():
            slugs.update(policy.models)
        return {s for s in slugs if not is_placeholder(s)}

    def chat_slugs(self) -> list[str]:
        """Models that may answer users: category models, in config order, de-duplicated."""
        seen: dict[str, None] = {}
        for policy in self.categories.values():
            for slug in policy.models:
                if not is_placeholder(slug):
                    seen.setdefault(slug, None)
        return list(seen)

    def placeholders(self) -> list[str]:
        found: list[str] = []
        if is_placeholder(self.classifier_model):
            found.append("classifier_model")
        if is_placeholder(self.baseline_model):
            found.append("baseline_model")
        for name, policy in self.categories.items():
            if any(is_placeholder(m) for m in policy.models):
                found.append(f"categories.{name}.models")
        return found


def load_routing_config(path: Path) -> RoutingConfig:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return RoutingConfig.model_validate(raw)


@lru_cache
def get_routing_config() -> RoutingConfig:
    return load_routing_config(get_settings().routing_config_path)

"""The router: filter -> classify -> policy -> stickiness (SPEC section 5).

Runs once per user message, before the model call. The tool loop that follows stays on
the chosen model.

One ordering detail differs from the SPEC 5.2/5.3 numbering: the hard filter runs after
classification, because the classifier's ``needs`` decide whether tool calling is required.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.llm.openrouter import OpenRouterClient
from app.models import CatalogModel
from app.router import classifier as clf
from app.router.config import RoutingConfig, ToolName
from app.router.filter import Requirements, apply_filter, has_native_file_input, rejection_reason
from app.router.policy import fallbacks_for, pick
from app.router.sticky import StickyState, decide

AttachmentKind = Literal["image", "pdf", "text", "spreadsheet", "other"]


@dataclass(frozen=True)
class AttachmentInfo:
    kind: AttachmentKind
    filename: str
    est_tokens: int = 0


@dataclass
class RouteRequest:
    text: str
    history: Sequence[dict[str, str]]
    attachments: Sequence[AttachmentInfo]
    est_tokens: int
    state: StickyState
    confidential: bool = False
    toggles: frozenset[str] = frozenset()  # "web_search", "code_execution", "connectors"
    override_model: str | None = None  # "Re-run with..." for this message only
    reuse: clf.Classification | None = None  # regeneration reuses the earlier classification


@dataclass
class RouteOutcome:
    model: str
    fallbacks: list[str]
    category: str
    confidence: float | None
    is_followup: bool
    needs: list[str]
    candidates: list[str]
    switched: bool
    forced: bool
    reason: str
    tools: set[ToolName]
    text: str  # message text after slash-command removal
    new_category: str
    native_file: bool = True
    classifier_latency_ms: int | None = None
    classifier_cost: Decimal | None = None
    classification: clf.Classification | None = None
    rejected: dict[str, str] = field(default_factory=dict)


class RoutingError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _tools_for(
    config: RoutingConfig,
    category: str,
    needs: set[str],
    toggles: frozenset[str],
    confidential: bool,
) -> set[ToolName]:
    category_tools = set(config.categories[category].tools)
    tools: set[ToolName] = set()
    if "run_python" in category_tools or "code_execution" in needs or "code_execution" in toggles:
        tools.add("run_python")
    # Connectors only for the actions category or when the user enables them (SPEC 8.4).
    if "connectors" in category_tools or "connectors" in toggles:
        tools.add("connectors")
    # Web search never runs in a confidential conversation (SPEC 8.1, 10).
    if not confidential and (
        "web_search" in category_tools or "web_search" in needs or "web_search" in toggles
    ):
        tools.add("web_search")
    return tools


async def _classify(
    req: RouteRequest,
    client: OpenRouterClient,
    config: RoutingConfig,
    pre: clf.PreRules,
    catalog: Mapping[str, CatalogModel],
) -> clf.Classification:
    if req.reuse is not None:
        return req.reuse
    if pre.forced_category:
        return clf.Classification(
            category=pre.forced_category,
            confidence=1.0,
            is_followup=False,
            needs=list(pre.needs),
            reason="chosen with a slash command",
            source="rule",
        )
    if pre.is_followup and req.state.current_category:
        return clf.Classification(
            category=req.state.current_category,
            confidence=1.0,
            is_followup=True,
            needs=list(pre.needs),
            reason="short follow-up to the previous answer",
            source="rule",
        )
    classifier_row = catalog.get(config.classifier_model)
    if rejection_reason(classifier_row, Requirements()) is not None:
        # SPEC 5.3: the classifier must itself pass the ZDR filter. Never call it otherwise.
        result = None
    else:
        result = await clf.classify(
            client,
            config,
            history=req.history,
            text=pre.text,
            attachments=[f"{a.filename} ({a.kind})" for a in req.attachments],
        )
    if result is not None:
        return result
    # Timeout or invalid JSON: keep the current model; new conversation -> default category.
    return clf.Classification(
        category=req.state.current_category or config.default_category,
        confidence=0.0,
        is_followup=req.state.current_model is not None,
        needs=list(pre.needs),
        reason="classifier unavailable, kept the current setup",
        source="fallback",
    )


async def route(
    req: RouteRequest,
    *,
    client: OpenRouterClient,
    config: RoutingConfig,
    catalog: Mapping[str, CatalogModel],
) -> RouteOutcome:
    has_image = any(a.kind == "image" for a in req.attachments)
    has_pdf = any(a.kind == "pdf" for a in req.attachments)
    pre = clf.apply_pre_rules(
        req.text, has_image=has_image, is_new_conversation=req.state.current_model is None
    )
    c = await _classify(req, client, config, pre, catalog)
    pinned = req.override_model or req.state.locked_model
    current = req.state.current_model

    # If stickiness will keep the current model (follow-up, or an unsure category change),
    # tools follow the category the conversation stays in. Otherwise an unsure guess at a
    # tool-using category would make the current model fail the filter and force a switch.
    staying = (
        not pinned
        and current is not None
        and req.state.current_category is not None
        and c.category != req.state.current_category
        and (c.is_followup or c.confidence < config.switch_threshold)
    )
    tool_category = req.state.current_category if staying else c.category
    assert tool_category is not None
    unsure_change = staying and not c.is_followup
    needs = set(pre.needs) | (set() if unsure_change else set(c.needs))
    if has_image:
        needs.add("vision")
    tools = _tools_for(config, tool_category, needs, req.toggles, req.confidential)

    requirements = Requirements(
        vision="vision" in needs,
        pdf=has_pdf,
        tools=bool(tools),
        est_tokens=req.est_tokens,
        context_safety_factor=config.context_safety_factor,
    )
    flt = apply_filter(config.chat_slugs(), catalog, requirements)
    if not flt.candidates:
        raise RoutingError(
            f"No model can take this message: it needs {requirements.describe()}, and none "
            "of the allowed models qualifies right now."
        )

    def outcome(
        model: str, fallbacks: list[str], *, switched: bool, forced: bool, reason: str, new_cat: str
    ) -> RouteOutcome:
        return RouteOutcome(
            model=model,
            fallbacks=fallbacks,
            category=c.category,
            confidence=c.confidence if c.source != "fallback" else None,
            is_followup=c.is_followup,
            needs=sorted(needs),
            candidates=list(flt.candidates),
            switched=switched,
            forced=forced,
            reason=reason,
            tools=tools,
            text=pre.text,
            new_category=new_cat,
            native_file=(not has_pdf) or has_native_file_input(catalog.get(model)),
            classifier_latency_ms=c.latency_ms,
            classifier_cost=c.cost,
            classification=c,
            rejected=flt.rejected,
        )

    if pinned:
        if not flt.passes(pinned):
            why = flt.rejected.get(pinned, "not in the allowed model list")
            what = "This chat is locked to" if not req.override_model else "You picked"
            raise RoutingError(
                f"{what} {pinned}, which can't take this message ({why}). "
                + ("Unlock the chat or change the message." if not req.override_model else "")
            )
        reason = "re-run with a model you picked" if req.override_model else "chat is locked"
        return outcome(
            pinned,
            [],
            switched=pinned != req.state.current_model and req.state.current_model is not None,
            forced=False,
            reason=reason,
            new_cat=c.category,
        )

    choice = pick(config, c.category, flt, catalog, prefer_native_file=has_pdf)
    if choice is None:  # unreachable while candidates is non-empty, kept for type narrowing
        raise RoutingError("No allowed model is available right now.")
    action = decide(
        req.state,
        category=c.category,
        confidence=c.confidence,
        is_followup=c.is_followup,
        current_passes_filter=flt.passes(current),
        policy_primary=choice.primary,
        switch_threshold=config.switch_threshold,
    )
    if action == "new":
        return outcome(
            choice.primary,
            choice.fallbacks,
            switched=False,
            forced=False,
            reason=c.reason,
            new_cat=c.category,
        )
    if action == "forced_switch":
        why = flt.rejected.get(current or "", "no longer allowed")
        return outcome(
            choice.primary,
            choice.fallbacks,
            switched=True,
            forced=True,
            reason=f"{current} can't take this message ({why})",
            new_cat=c.category,
        )
    if action == "switch":
        return outcome(
            choice.primary,
            choice.fallbacks,
            switched=True,
            forced=False,
            reason=c.reason,
            new_cat=c.category,
        )
    # keep
    assert current is not None
    keep_category = req.state.current_category or c.category
    return outcome(
        current,
        fallbacks_for(current, config, keep_category, flt),
        switched=False,
        forced=False,
        reason=c.reason,
        new_cat=keep_category,
    )

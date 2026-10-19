"""Step 2 of routing: deterministic pre-rules, then the classifier model (SPEC section 5.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from app.llm.openrouter import OpenRouterClient, OpenRouterError
from app.router.config import CATEGORIES, RoutingConfig

logger = logging.getLogger(__name__)

Need = Literal["web_search", "code_execution", "connectors", "vision", "long_context"]
NEEDS: tuple[Need, ...] = ("web_search", "code_execution", "connectors", "vision", "long_context")

SLASH_COMMANDS: dict[str, str] = {
    "/code": "coding",
    "/data": "data_analysis",
    "/research": "research",
    "/search": "research",
    "/doc": "document_qa",
    "/write": "writing",
    "/think": "reasoning",
    "/act": "actions",
    "/general": "general",
}

# Short messages that only make sense as a follow-up to the previous answer.
FOLLOWUP_PHRASES = {
    "again",
    "continue",
    "go on",
    "more",
    "shorter",
    "longer",
    "simpler",
    "translate it",
    "make it shorter",
    "make it longer",
    "more formal",
    "less formal",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "yes",
    "no",
    "why",
    "why?",
    "explain",
    "retry",
    "try again",
}
FOLLOWUP_MAX_WORDS = 3
FOLLOWUP_LEAD_WORDS = {
    "shorter",
    "again",
    "translate",
    "more",
    "continue",
    "rephrase",
    "summarise",
    "summarize",
}
HISTORY_TURNS = 4
HISTORY_CHARS = 1500  # per message, keeps the classifier fast


@dataclass
class Classification:
    category: str
    confidence: float
    is_followup: bool
    needs: list[str] = field(default_factory=list)
    reason: str = ""
    source: Literal["classifier", "rule", "fallback"] = "classifier"
    latency_ms: int | None = None
    cost: Decimal | None = None


@dataclass
class PreRules:
    """Outcome of the cheap deterministic rules that run before the classifier."""

    forced_category: str | None = None
    is_followup: bool = False
    needs: list[str] = field(default_factory=list)
    text: str = ""  # message text with any slash command removed


def apply_pre_rules(text: str, *, has_image: bool, is_new_conversation: bool) -> PreRules:
    rules = PreRules(text=text)
    if has_image:
        rules.needs.append("vision")
    stripped = text.strip()
    match = re.match(r"^(/[a-z]+)\b\s*", stripped, flags=re.IGNORECASE)
    if match and match.group(1).lower() in SLASH_COMMANDS:
        rules.forced_category = SLASH_COMMANDS[match.group(1).lower()]
        rules.text = stripped[match.end() :]
        return rules
    if not is_new_conversation:
        normalized = re.sub(r"[.!]+$", "", stripped.lower())
        words = normalized.split()
        rules.is_followup = (
            not normalized
            or normalized in FOLLOWUP_PHRASES
            or (len(words) <= FOLLOWUP_MAX_WORDS and words[0] in FOLLOWUP_LEAD_WORDS)
        )
    return rules


def _response_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "route",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "confidence", "is_followup", "needs", "reason"],
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "confidence": {"type": "number"},
                    "is_followup": {"type": "boolean"},
                    "needs": {"type": "array", "items": {"type": "string", "enum": list(NEEDS)}},
                    "reason": {"type": "string"},
                },
            },
        },
    }


def build_prompt(
    config: RoutingConfig, history: Sequence[dict[str, str]], text: str, attachments: Sequence[str]
) -> list[dict[str, str]]:
    lines = [
        "You route chat messages for a team assistant. Pick exactly one category for the NEW "
        "message, using the conversation only as context.",
        "",
        "Categories (mutually exclusive):",
    ]
    for name, policy in config.categories.items():
        examples = "; ".join(f'"{e}"' for e in policy.examples[:3])
        lines.append(
            f"- {name}: {policy.description}" + (f" Examples: {examples}" if examples else "")
        )
    lines += [
        "",
        "needs may include: web_search (current facts from the internet), code_execution "
        "(running Python on data), connectors (the user's Gmail, Drive or CRM), vision "
        "(images), long_context (very long documents).",
        "is_followup is true when the new message continues or adjusts the previous answer "
        "(e.g. 'shorter', 'now in Hindi', 'add a table') rather than starting a new task.",
        "confidence is your probability (0 to 1) that the category is right.",
        "reason is one short sentence, no more than 15 words.",
        "Reply with JSON only.",
    ]
    convo = []
    for turn in history[-HISTORY_TURNS:]:
        convo.append(f"{turn['role'].upper()}: {turn['text'][:HISTORY_CHARS]}")
    user = ""
    if convo:
        user += "Conversation so far:\n" + "\n".join(convo) + "\n\n"
    if attachments:
        user += "Attachments on the new message: " + ", ".join(attachments) + "\n\n"
    user += f"NEW MESSAGE:\n{text[:6000]}"
    return [{"role": "system", "content": "\n".join(lines)}, {"role": "user", "content": user}]


def parse_classification(raw: str) -> Classification | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)  # tolerate fenced JSON
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict) or data.get("category") not in CATEGORIES:
        return None
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        return None
    needs = [n for n in data.get("needs") or [] if n in NEEDS]
    return Classification(
        category=str(data["category"]),
        confidence=confidence,
        is_followup=bool(data.get("is_followup", False)),
        needs=needs,
        reason=str(data.get("reason") or "")[:300],
    )


async def classify(
    client: OpenRouterClient,
    config: RoutingConfig,
    *,
    history: Sequence[dict[str, str]],
    text: str,
    attachments: Sequence[str] = (),
) -> Classification | None:
    """One classifier call; None on timeout, error or invalid JSON (caller keeps the model)."""
    started = time.monotonic()
    try:
        async with asyncio.timeout(config.classifier_timeout_s):
            result = await client.complete(
                model=config.classifier_model,
                messages=build_prompt(config, history, text, attachments),
                response_format=_response_schema(),
                temperature=0,
                max_tokens=200,
                request_timeout_s=config.classifier_timeout_s,
                retry=False,  # never more than one classifier call per message (SPEC 5.7)
                provider={"require_parameters": True},  # only providers honouring the schema
            )
    except (TimeoutError, OpenRouterError) as err:
        logger.info("classifier unavailable: %s", type(err).__name__)
        return None
    parsed = parse_classification(result.text)
    if parsed is None:
        logger.info("classifier returned invalid JSON")
        return None
    parsed.latency_ms = int((time.monotonic() - started) * 1000)
    parsed.cost = result.usage.cost if result.usage else None
    return parsed

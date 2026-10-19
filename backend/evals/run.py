"""Eval harness: router vs. "always use the baseline model" (SPEC sections 2 and 15).

    uv run python -m evals.run --set evals/prompts.yaml

For each prompt, (a) the router picks a model as for a new conversation and that model
answers, (b) ``baseline_model`` answers. A pairwise judge (``judge_model``, ZDR like every
call) sees both answers in random order and says which is better or tie. Both arms get the
same OpenRouter server tools (web search when the router enables it); local tools
(run_python, connectors) are not used, so `actions` prompts measure only how each model
handles a request it cannot act on.

The report is written as markdown: win/tie/loss and cost per category, and the largest
losses with both answers. LLM judges have biases: a person should spot-check results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.agent.history import system_prompt
from app.agent.tools.web_search import web_search_tool
from app.core.config import get_settings
from app.llm.catalog import model_rows
from app.llm.openrouter import Completion, OpenRouterClient, OpenRouterError
from app.models import CatalogModel
from app.router.config import RoutingConfig, is_placeholder, load_routing_config
from app.router.decision import RouteRequest, RoutingError, route
from app.router.sticky import StickyState

Verdict = Literal["router", "baseline", "tie"]
CHARS_PER_TOKEN = 4


class EvalPrompt(BaseModel):
    id: str
    category: str
    prompt: str


class EvalSet(BaseModel):
    version: int = 1
    prompts: list[EvalPrompt] = Field(min_length=1)


@dataclass
class Arm:
    model: str
    text: str = ""
    cost: Decimal = Decimal(0)
    error: str | None = None


@dataclass
class ItemResult:
    item: EvalPrompt
    routed_category: str = ""
    router: Arm = field(default_factory=lambda: Arm(""))
    baseline: Arm = field(default_factory=lambda: Arm(""))
    verdict: Verdict = "tie"
    margin: int = 0  # 0 tie .. 5 decisive
    reason: str = ""
    judge_cost: Decimal = Decimal(0)
    same_model: bool = False


JUDGE_SYSTEM = (
    "You are an impartial expert judge comparing two assistant answers to the same request "
    "from a small industrial sales team in India. Judge correctness first, then usefulness, "
    "completeness and clarity. Ignore length unless it hurts usefulness. Do not favour an "
    "answer because of its position. Reply with JSON only."
)

JUDGE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["winner", "margin", "reason"],
            "properties": {
                "winner": {"type": "string", "enum": ["A", "B", "tie"]},
                "margin": {"type": "integer", "minimum": 0, "maximum": 5},
                "reason": {"type": "string"},
            },
        },
    },
}


def judge_messages(prompt: str, first: str, second: str) -> list[dict[str, str]]:
    user = (
        f"REQUEST:\n{prompt}\n\n=== ANSWER A ===\n{first}\n\n=== ANSWER B ===\n{second}\n\n"
        'Which answer is better? Reply as {"winner": "A"|"B"|"tie", "margin": 0-5 '
        '(0 = tie, 5 = one answer is clearly unusable), "reason": "<one sentence>"}.'
    )
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_judge(raw: str) -> tuple[str, int, str] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    winner = data.get("winner") if isinstance(data, dict) else None
    if winner not in ("A", "B", "tie"):
        return None
    try:
        margin = max(0, min(5, int(data.get("margin", 0))))
    except (TypeError, ValueError):
        margin = 0
    return str(winner), margin, str(data.get("reason") or "")[:500]


class Evaluator:
    def __init__(
        self,
        client: OpenRouterClient,
        config: RoutingConfig,
        catalog: dict[str, CatalogModel],
        *,
        seed: int = 7,
        both_orders: bool = False,
    ) -> None:
        if not config.judge_model or is_placeholder(config.judge_model):
            raise SystemExit("routing.yaml needs a real judge_model for evals.")
        if is_placeholder(config.baseline_model):
            raise SystemExit("routing.yaml needs a real baseline_model for evals.")
        self.client = client
        self.config = config
        self.catalog = catalog
        self.rng = random.Random(seed)  # noqa: S311 (answer order, not security)
        self.both_orders = both_orders

    async def _answer(self, model: str, fallbacks: list[str], prompt: str, tools: list[Any]) -> Arm:
        arm = Arm(model)
        messages = [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": prompt},
        ]
        completion: Completion | None = None
        try:
            async for event in self.client.stream_chat(
                model=model, messages=messages, fallbacks=fallbacks, tools=tools or None
            ):
                if isinstance(event, Completion):
                    completion = event
        except OpenRouterError as err:
            arm.error = err.user_message
            return arm
        assert completion is not None
        arm.model = completion.model
        arm.text = completion.text
        if completion.usage and completion.usage.cost is not None:
            arm.cost = completion.usage.cost
        return arm

    async def _judge_once(
        self, prompt: str, router: str, baseline: str
    ) -> tuple[Verdict, int, str, Decimal]:
        router_first = self.rng.random() < 0.5
        a, b = (router, baseline) if router_first else (baseline, router)
        result = await self.client.complete(
            model=self.config.judge_model or "",
            messages=judge_messages(prompt, a, b),
            response_format=JUDGE_SCHEMA,
            temperature=0,
            max_tokens=300,
            provider={"require_parameters": True},
        )
        cost = result.usage.cost if result.usage and result.usage.cost else Decimal(0)
        parsed = parse_judge(result.text)
        if parsed is None:
            return "tie", 0, "judge returned invalid JSON", cost
        winner, margin, reason = parsed
        if winner == "tie":
            return "tie", 0, reason, cost
        router_won = (winner == "A") == router_first
        return ("router" if router_won else "baseline"), margin, reason, cost

    async def run_item(self, item: EvalPrompt) -> ItemResult:
        res = ItemResult(item=item)
        state = StickyState(current_model=None, current_category=None, locked_model=None)
        req = RouteRequest(
            text=item.prompt,
            history=[],
            attachments=[],
            est_tokens=len(item.prompt) // CHARS_PER_TOKEN + 400,
            state=state,
        )
        try:
            outcome = await route(req, client=self.client, config=self.config, catalog=self.catalog)
        except RoutingError as err:
            res.router.error = err.user_message
            res.verdict, res.reason = "baseline", f"router error: {err.user_message}"
            return res
        res.routed_category = outcome.category
        tools = [web_search_tool()] if "web_search" in outcome.tools else []
        res.router, res.baseline = await asyncio.gather(
            self._answer(outcome.model, outcome.fallbacks, outcome.text, tools),
            self._answer(self.config.baseline_model, [], item.prompt, tools),
        )
        res.router.cost += outcome.classifier_cost or Decimal(0)
        if res.router.error or res.baseline.error:
            res.verdict = (
                "baseline"
                if res.router.error and not res.baseline.error
                else ("router" if res.baseline.error and not res.router.error else "tie")
            )
            res.reason = f"error: router={res.router.error} baseline={res.baseline.error}"
            return res
        if outcome.model == self.config.baseline_model:
            res.same_model, res.verdict, res.reason = True, "tie", "router chose the baseline model"
            return res

        verdict, margin, reason, cost = await self._judge_once(
            item.prompt, res.router.text, res.baseline.text
        )
        res.judge_cost += cost
        if self.both_orders:
            v2, m2, _, cost2 = await self._judge_once(
                item.prompt, res.router.text, res.baseline.text
            )
            res.judge_cost += cost2
            if v2 != verdict:
                verdict, margin, reason = "tie", 0, f"judge disagreed across orders ({reason})"
            else:
                margin = min(margin, m2)
        res.verdict, res.margin, res.reason = verdict, margin, reason
        return res

    async def run(self, items: list[EvalPrompt], concurrency: int = 4) -> list[ItemResult]:
        sem = asyncio.Semaphore(concurrency)

        async def one(item: EvalPrompt) -> ItemResult:
            async with sem:
                result = await self.run_item(item)
                print(f"  {item.id}: {result.verdict}", file=sys.stderr)
                return result

        return list(await asyncio.gather(*(one(i) for i in items)))


def _quote(text: str, limit: int = 3000) -> str:
    body = text if len(text) <= limit else text[:limit] + "\n[... truncated]"
    return "\n".join("> " + line for line in body.splitlines()) or "> (empty)"


def render_report(results: list[ItemResult], config: RoutingConfig, *, largest: int = 10) -> str:
    by_cat: dict[str, list[ItemResult]] = defaultdict(list)
    for r in results:
        by_cat[r.item.category].append(r)

    def wtl(rows: list[ItemResult]) -> tuple[int, int, int]:
        return (
            sum(r.verdict == "router" for r in rows),
            sum(r.verdict == "tie" for r in rows),
            sum(r.verdict == "baseline" for r in rows),
        )

    w, t, l_ = wtl(results)
    router_cost = sum((r.router.cost for r in results), Decimal(0))
    base_cost = sum((r.baseline.cost for r in results), Decimal(0))
    judge_cost = sum((r.judge_cost for r in results), Decimal(0))
    lines = [
        "# Relay routing eval",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Baseline: `{config.baseline_model}`. "
        f"Judge: `{config.judge_model}`. Prompts: {len(results)}.",
        "",
        f"**Overall (router vs baseline): {w} wins / {t} ties / {l_} losses.** "
        f"Router cost ${router_cost:.4f} (incl. classifier), baseline ${base_cost:.4f}, "
        f"judge ${judge_cost:.4f}.",
        "",
        "Decision rule (SPEC section 2): if the router does not beat or match the baseline on "
        "the team's own prompts, set every category to the baseline model.",
        "",
        "## Per category",
        "",
        "| Category | n | W | T | L | Router cost | Baseline cost | Routed as expected "
        "| Same model |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cat in sorted(by_cat):
        rows = by_cat[cat]
        cw, ct, cl = wtl(rows)
        rc = sum((r.router.cost for r in rows), Decimal(0))
        bc = sum((r.baseline.cost for r in rows), Decimal(0))
        correct = sum(r.routed_category == cat for r in rows)
        same = sum(r.same_model for r in rows)
        lines.append(
            f"| {cat} | {len(rows)} | {cw} | {ct} | {cl} | ${rc:.4f} | ${bc:.4f} | "
            f"{correct}/{len(rows)} | {same} |"
        )

    losses = sorted(
        (r for r in results if r.verdict == "baseline"), key=lambda r: r.margin, reverse=True
    )[:largest]
    lines += ["", f"## Largest losses ({len(losses)})", ""]
    if not losses:
        lines.append("None.")
    for r in losses:
        lines += [
            f"### {r.item.id} ({r.item.category}, routed as {r.routed_category or '-'}), "
            f"margin {r.margin}",
            "",
            f"Judge: {r.reason}",
            "",
            "**Prompt**",
            "",
            _quote(r.item.prompt, 1500),
            "",
            f"**Router answer** (`{r.router.model}`)",
            "",
            _quote(r.router.error or r.router.text),
            "",
            f"**Baseline answer** (`{r.baseline.model}`)",
            "",
            _quote(r.baseline.error or r.baseline.text),
            "",
        ]
    lines += [
        "## All items",
        "",
        "| id | category | routed | router model | verdict | margin | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: r.item.id):
        reason = r.reason.replace("|", "/").replace("\n", " ")[:160]
        lines.append(
            f"| {r.item.id} | {r.item.category} | {r.routed_category} | `{r.router.model}` | "
            f"{r.verdict} | {r.margin} | {reason} |"
        )
    return "\n".join(lines) + "\n"


async def live_catalog(client: OpenRouterClient) -> dict[str, CatalogModel]:
    models = await client.list_user_models()
    zdr = await client.list_zdr_endpoints()
    return {r["slug"]: CatalogModel(**r) for r in model_rows(models, zdr, datetime.now(UTC))}


async def main_async(args: argparse.Namespace) -> Path:
    settings = get_settings()
    config = load_routing_config(
        Path(args.routing) if args.routing else settings.routing_config_path
    )
    raw_set = await asyncio.to_thread(Path(args.set).read_text, encoding="utf-8")
    eval_set = EvalSet.model_validate(yaml.safe_load(raw_set))
    items = eval_set.prompts
    if args.categories:
        wanted = set(args.categories.split(","))
        items = [i for i in items if i.category in wanted]
    if args.limit:
        items = items[: args.limit]
    client = OpenRouterClient(settings.openrouter_api_key, settings.openrouter_base_url)
    try:
        catalog = await live_catalog(client)
        evaluator = Evaluator(client, config, catalog, seed=args.seed, both_orders=args.both_orders)
        results = await evaluator.run(items, concurrency=args.concurrency)
    finally:
        await client.aclose()
    out = (
        Path(args.out)
        if args.out
        else Path("evals/reports") / f"eval-{datetime.now(UTC):%Y%m%d-%H%M}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(results, config), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="evals.run", description=__doc__.split("\n\n")[0])
    parser.add_argument("--set", required=True, help="YAML prompt set")
    parser.add_argument("--out", help="report path (default evals/reports/eval-<time>.md)")
    parser.add_argument("--routing", help="routing.yaml (default: ROUTING_CONFIG_PATH)")
    parser.add_argument("--categories", help="comma-separated categories to include")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed", type=int, default=7, help="seed for answer order")
    parser.add_argument(
        "--both-orders",
        action="store_true",
        help="judge each pair twice with swapped order; disagreement counts as a tie",
    )
    out = asyncio.run(main_async(parser.parse_args(argv)))
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()

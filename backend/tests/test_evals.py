"""Eval harness tests with OpenRouter mocked."""

import json
from pathlib import Path

import httpx
import respx

from evals.run import main_async, parse_judge
from tests.fakes import (
    BASE_URL,
    CHAT_URL,
    FAKE_MODELS,
    FAKE_ZDR,
    json_completion,
    sse_response,
    text_stream,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "routing.yaml"


def test_parse_judge() -> None:
    assert parse_judge('{"winner": "B", "margin": 4, "reason": "more accurate"}') == (
        "B",
        4,
        "more accurate",
    )
    assert parse_judge('```json\n{"winner": "tie", "margin": 0, "reason": ""}\n```') == (
        "tie",
        0,
        "",
    )
    assert parse_judge("A is better") is None
    assert parse_judge('{"winner": "C"}') is None


class FakeEvalAPI:
    """Classifier says `coding` for code prompts else `general`; the judge always prefers
    the answer that came from the routed model, wherever it is placed."""

    def __init__(self) -> None:
        self.judge_calls = 0
        self.bodies: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        model = body["model"]
        assert body["provider"]["zdr"] is True
        assert body["provider"]["data_collection"] == "deny"
        if model == "acme/fast-classifier":
            text = body["messages"][-1]["content"]
            category = "coding" if "python" in text.lower() else "general"
            verdict = {"category": category, "confidence": 0.9, "is_followup": False,
                       "needs": [], "reason": "x"}  # fmt: skip
            return httpx.Response(200, json=json_completion(json.dumps(verdict), model=model))
        if model == "acme/thinker" and not body.get("stream"):
            self.judge_calls += 1
            user = body["messages"][-1]["content"]
            a = user.split("=== ANSWER A ===")[1].split("=== ANSWER B ===")[0]
            winner = "A" if "ROUTED" in a else "B"
            verdict = {"winner": winner, "margin": 3, "reason": "routed answer is better"}
            return httpx.Response(200, json=json_completion(json.dumps(verdict), model=model))
        label = "BASELINE" if model == "acme/allround" else "ROUTED"
        return sse_response(text_stream(f"{label} answer from {model}", model=model, cost=0.01))


@respx.mock
async def test_eval_run_writes_report(tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/models/user").mock(
        return_value=httpx.Response(200, json={"data": FAKE_MODELS})
    )
    respx.get(f"{BASE_URL}/endpoints/zdr").mock(
        return_value=httpx.Response(200, json={"data": FAKE_ZDR})
    )
    api = FakeEvalAPI()
    respx.post(CHAT_URL).mock(side_effect=api)
    eval_set = tmp_path / "set.yaml"
    eval_set.write_text(
        "version: 1\nprompts:\n"
        "  - {id: c1, category: coding, prompt: 'Write python to sum a list'}\n"
        "  - {id: c2, category: coding, prompt: 'Write python to parse GSTIN'}\n"
        "  - {id: g1, category: general, prompt: 'What is AMC?'}\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.md"

    class Args:
        set = str(eval_set)
        routing = str(FIXTURE)
        out = str(tmp_path / "report.md")
        categories = None
        limit = 0
        concurrency = 2
        seed = 3
        both_orders = True

    path = await main_async(Args())  # type: ignore[arg-type]
    report = path.read_text(encoding="utf-8")
    assert path == out
    # Coding prompts route to acme/coder and win; general routes to the baseline (tie).
    assert "**Overall (router vs baseline): 2 wins / 1 ties / 0 losses.**" in report
    assert "| coding | 2 | 2 | 0 | 0 |" in report
    assert "router chose the baseline model" in report
    assert api.judge_calls == 4  # both orders for the two non-baseline items
    judge_bodies = [b for b in api.bodies if b["model"] == "acme/thinker"]
    assert all(b["response_format"]["type"] == "json_schema" for b in judge_bodies)  # type: ignore[index]

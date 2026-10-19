"""Router tests (SPEC section 5, TASKS Phase 3)."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.llm.catalog import model_rows
from app.llm.openrouter import OpenRouterClient
from app.models import CatalogModel
from app.router.config import RoutingConfig, load_routing_config
from app.router.decision import AttachmentInfo, RouteRequest, RoutingError, route
from app.router.sticky import StickyState
from tests.fakes import BASE_URL, CHAT_URL, FAKE_MODELS, FAKE_ZDR, json_completion, request_body

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "routing.yaml"
NEW = StickyState(current_model=None, current_category=None, locked_model=None)


@pytest.fixture
def config() -> RoutingConfig:
    return load_routing_config(FIXTURE)


@pytest.fixture
def catalog() -> dict[str, CatalogModel]:
    rows = model_rows(FAKE_MODELS, FAKE_ZDR, datetime.now(UTC))
    return {r["slug"]: CatalogModel(**r) for r in rows}


@pytest.fixture
async def client() -> AsyncIterator[OpenRouterClient]:
    c = OpenRouterClient("k", BASE_URL)
    yield c
    await c.aclose()


def classifier_says(
    category: str,
    confidence: float = 0.9,
    is_followup: bool = False,
    needs: list[str] | None = None,
) -> respx.Route:
    payload = {
        "category": category,
        "confidence": confidence,
        "is_followup": is_followup,
        "needs": needs or [],
        "reason": f"looks like {category}",
    }
    return respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=json_completion(json.dumps(payload)))
    )


def req(text: str, state: StickyState = NEW, **kw: Any) -> RouteRequest:
    return RouteRequest(
        text=text,
        history=kw.pop("history", []),
        attachments=kw.pop("attachments", []),
        est_tokens=kw.pop("est_tokens", 1000),
        state=state,
        **kw,
    )


async def run(
    r: RouteRequest, client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, Any]
) -> Any:
    return await route(r, client=client, config=config, catalog=catalog)


@respx.mock
async def test_new_conversation_uses_category_primary_with_two_fallbacks(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding")
    out = await run(req("Fix my FastAPI endpoint"), client, config, catalog)
    assert out.model == "acme/coder"
    assert out.fallbacks == ["acme/allround", "acme/vision-pro"]
    assert out.category == "coding"
    assert out.switched is False and out.forced is False
    assert "run_python" in out.tools


@respx.mock
async def test_classifier_request_is_private_and_called_once(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    route_ = classifier_says("writing")
    await run(req("Draft an email to Hindalco"), client, config, catalog)
    assert route_.call_count == 1
    body = request_body(route_.calls[0])
    assert body["model"] == "acme/fast-classifier"
    assert body["provider"]["zdr"] is True and body["provider"]["data_collection"] == "deny"
    assert body["response_format"]["type"] == "json_schema"


@respx.mock
async def test_followup_keeps_model(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("reasoning", 0.95, is_followup=True)
    state = StickyState("acme/writer", "writing", None)
    out = await run(
        req("now add a table comparing the two options", state), client, config, catalog
    )
    assert out.model == "acme/writer"
    assert out.switched is False
    assert out.new_category == "writing"


@respx.mock
async def test_short_followup_skips_classifier(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    route_ = classifier_says("coding")
    state = StickyState("acme/writer", "writing", None)
    out = await run(req("shorter", state), client, config, catalog)
    assert route_.call_count == 0
    assert out.model == "acme/writer" and out.is_followup is True


@respx.mock
async def test_low_confidence_category_change_keeps_model(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding", confidence=0.6)
    state = StickyState("acme/writer", "writing", None)
    out = await run(
        req("can you write a regex for invoice numbers", state), client, config, catalog
    )
    assert out.model == "acme/writer"
    assert out.switched is False
    assert out.new_category == "writing"


@respx.mock
async def test_confident_category_change_switches(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding", confidence=0.9)
    state = StickyState("acme/thinker", "reasoning", None)
    out = await run(req("write a SQL query for monthly revenue", state), client, config, catalog)
    assert out.model == "acme/coder"
    assert out.switched is True and out.forced is False
    assert out.new_category == "coding"


@respx.mock
async def test_same_category_keeps_model_even_if_policy_differs(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding", confidence=0.99)
    state = StickyState("acme/allround", "coding", None)
    out = await run(req("now add error handling", state), client, config, catalog)
    assert out.model == "acme/allround"
    assert out.switched is False


@respx.mock
async def test_image_attachment_forces_switch_to_vision_model(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("writing", confidence=0.9, is_followup=True)
    state = StickyState("acme/writer", "writing", None)
    out = await run(
        req("describe this", state, attachments=[AttachmentInfo("image", "photo.png", 800)]),
        client,
        config,
        catalog,
    )
    assert out.model == "acme/allround"  # writing list: writer (no vision) -> allround
    assert out.switched is True and out.forced is True
    assert "image" in out.reason
    assert "vision" in out.needs


@respx.mock
async def test_locked_model_is_respected(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding", confidence=0.99)
    state = StickyState("acme/writer", "writing", "acme/thinker")
    out = await run(req("write python to parse this", state), client, config, catalog)
    assert out.model == "acme/thinker"
    assert out.fallbacks == []


@respx.mock
async def test_locked_model_failing_filter_says_why(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("general")
    state = StickyState("acme/thinker", "reasoning", "acme/thinker")
    with pytest.raises(RoutingError) as exc:
        await run(
            req("what is this", state, attachments=[AttachmentInfo("image", "a.png", 500)]),
            client,
            config,
            catalog,
        )
    assert "locked to acme/thinker" in exc.value.user_message
    assert "no image input" in exc.value.user_message


@respx.mock
async def test_filter_failure_gives_clear_error(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("document_qa")
    with pytest.raises(RoutingError) as exc:
        await run(
            req(
                "summarise",
                attachments=[AttachmentInfo("image", "scan.png", 0)],
                est_tokens=900_000,
            ),
            client,
            config,
            catalog,
        )
    msg = exc.value.user_message
    assert "zero-data-retention" in msg and "vision" in msg and "1080k context" in msg


@respx.mock
async def test_classifier_timeout_keeps_current_model(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    attempts = 0

    async def slow(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json=json_completion("{}"))

    respx.post(CHAT_URL).mock(side_effect=slow)
    fast = config.model_copy(update={"classifier_timeout_s": 0.05})
    state = StickyState("acme/thinker", "reasoning", None)
    out = await run(req("write python code for this", state), client, fast, catalog)
    assert out.model == "acme/thinker"
    assert out.switched is False
    assert attempts == 1  # no retry


@respx.mock
async def test_classifier_timeout_on_new_conversation_uses_default_category(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    out = await run(req("hello there, how are you"), client, config, catalog)
    assert out.category == "general"
    assert out.model == "acme/allround"


@respx.mock
async def test_invalid_classifier_json_keeps_current_model(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=json_completion("I think it is coding"))
    )
    state = StickyState("acme/writer", "writing", None)
    out = await run(req("write a SQL query", state), client, config, catalog)
    assert out.model == "acme/writer"


@respx.mock
async def test_slash_command_forces_category_without_classifier(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    route_ = classifier_says("writing")
    state = StickyState("acme/writer", "writing", None)
    out = await run(req("/code make this faster", state), client, config, catalog)
    assert route_.call_count == 0
    assert out.category == "coding" and out.model == "acme/coder"
    assert out.text == "make this faster"


@respx.mock
async def test_never_routes_outside_allowlist_or_to_non_zdr(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("general")
    out = await run(req("hi"), client, config, catalog)
    assert "other/no-zdr" not in out.candidates
    assert set(out.candidates) <= set(config.chat_slugs())


@respx.mock
async def test_model_without_zdr_endpoint_is_filtered(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding")
    catalog["acme/coder"].has_zdr_endpoint = False
    out = await run(req("debug this"), client, config, catalog)
    assert out.model == "acme/allround"
    assert out.rejected["acme/coder"] == "no zero-data-retention endpoint"


@respx.mock
async def test_confidential_conversation_never_gets_web_search(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("research", needs=["web_search"])
    out = await run(
        req("latest genset prices", confidential=True, toggles=frozenset({"web_search"})),
        client,
        config,
        catalog,
    )
    assert "web_search" not in out.tools


@respx.mock
async def test_research_gets_web_search_when_not_confidential(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("research")
    out = await run(req("latest genset prices"), client, config, catalog)
    assert out.tools == {"web_search"}
    assert out.model == "acme/searcher"


@respx.mock
async def test_tool_need_filters_models_without_tool_calling(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("writing", needs=["code_execution"])
    out = await run(req("draft and compute the totals"), client, config, catalog)
    assert out.model == "acme/allround"  # acme/writer has no tool calling
    assert "run_python" in out.tools


@respx.mock
async def test_pdf_prefers_native_file_model(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding")
    out = await run(
        req("review the code in this pdf", attachments=[AttachmentInfo("pdf", "a.pdf", 5000)]),
        client,
        config,
        catalog,
    )
    assert out.model == "acme/allround"  # coder lacks native file input
    assert out.native_file is True


@respx.mock
async def test_override_model_used_for_rerun(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding")
    state = StickyState("acme/coder", "coding", None)
    out = await run(req("again", state, override_model="acme/thinker"), client, config, catalog)
    assert out.model == "acme/thinker" and out.fallbacks == []


@respx.mock
async def test_override_outside_allowlist_rejected(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding")
    with pytest.raises(RoutingError):
        await run(req("x", override_model="other/no-zdr"), client, config, catalog)


@respx.mock
async def test_confident_switch_away_from_model_without_tools_is_forced(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding", confidence=0.9)
    state = StickyState("acme/writer", "writing", None)
    out = await run(req("write a SQL query for monthly revenue", state), client, config, catalog)
    assert out.model == "acme/coder"
    assert out.switched is True and out.forced is True
    assert "no tool calling" in out.reason


@respx.mock
async def test_unsure_change_does_not_add_new_category_tools(
    client: OpenRouterClient, config: RoutingConfig, catalog: dict[str, CatalogModel]
) -> None:
    classifier_says("coding", confidence=0.5, needs=["code_execution"])
    state = StickyState("acme/writer", "writing", None)
    out = await run(req("hmm what about a regex", state), client, config, catalog)
    assert out.model == "acme/writer"
    assert out.tools == set()

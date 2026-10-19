"""OpenRouter client tests. SPEC section 15: no request reaches OpenRouter without the
privacy parameters. Never weaken these tests (CLAUDE.md hard rule 1)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.llm.openrouter import Completion, OpenRouterClient, OpenRouterError, TextDelta
from tests.fakes import BASE_URL, CHAT_URL, json_completion, request_body, sse, sse_response
from tests.fakes import text_stream as make_stream


@pytest.fixture
async def client() -> AsyncIterator[OpenRouterClient]:
    c = OpenRouterClient("test-key", BASE_URL)
    yield c
    await c.aclose()


async def _drain(gen: AsyncIterator[Any]) -> list[Any]:
    return [e async for e in gen]


def _assert_private(body: dict[str, Any]) -> None:
    assert body["provider"]["zdr"] is True
    assert body["provider"]["data_collection"] == "deny"


Call = Callable[[OpenRouterClient], Awaitable[object]]

CALL_PATHS: dict[str, Call] = {
    "stream_plain": lambda c: _drain(c.stream_chat(model="a/b", messages=[])),
    "stream_fallbacks_tools_plugins": lambda c: _drain(
        c.stream_chat(
            model="a/b",
            messages=[{"role": "user", "content": "hi"}],
            fallbacks=["c/d", "e/f"],
            tools=[{"type": "openrouter:web_search"}],
            plugins=[{"id": "file-parser"}],
        )
    ),
    "stream_caller_tries_to_disable": lambda c: _drain(
        c.stream_chat(
            model="a/b",
            messages=[],
            provider={"zdr": False, "data_collection": "allow", "sort": "throughput"},
            extra={"provider": {"zdr": False}},
        )
    ),
    "complete_plain": lambda c: c.complete(model="a/b", messages=[]),
    "complete_json_schema": lambda c: c.complete(
        model="a/b",
        messages=[],
        response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        provider={"require_parameters": True},
    ),
    "complete_caller_tries_to_disable": lambda c: c.complete(
        model="a/b", messages=[], provider={"zdr": False, "data_collection": "allow"}
    ),
    "complete_no_retry": lambda c: c.complete(model="a/b", messages=[], retry=False),
}


@pytest.mark.parametrize("name", sorted(CALL_PATHS))
@respx.mock
async def test_every_chat_request_carries_privacy_params(
    client: OpenRouterClient, name: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if b'"stream":true' in request.content.replace(b" ", b""):
            return sse_response(make_stream("ok"))
        return httpx.Response(200, json=json_completion("ok"))

    route = respx.post(CHAT_URL).mock(side_effect=respond)
    await CALL_PATHS[name](client)
    assert route.called
    for call in route.calls:
        _assert_private(request_body(call))


@respx.mock
async def test_caller_provider_extras_are_kept(client: OpenRouterClient) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=json_completion("x")))
    await client.complete(model="a/b", messages=[], provider={"require_parameters": True})
    body = request_body(route.calls[0])
    assert body["provider"]["require_parameters"] is True
    _assert_private(body)


@respx.mock
async def test_stream_yields_text_then_completion_with_usage(client: OpenRouterClient) -> None:
    respx.post(CHAT_URL).mock(
        return_value=sse_response(make_stream("Hello world", model="acme/fallback-x", cost=0.0042))
    )
    events = await _drain(client.stream_chat(model="acme/primary", messages=[]))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    done = events[-1]
    assert text == "Hello world"
    assert isinstance(done, Completion)
    assert done.text == "Hello world"
    # The answered model comes from the response, not the request (CLAUDE.md rule 4).
    assert done.model == "acme/fallback-x"
    assert done.finish_reason == "stop"
    assert done.usage is not None
    assert done.usage.cost == Decimal("0.0042")
    assert (done.usage.prompt_tokens, done.usage.completion_tokens) == (10, 5)
    assert done.usage.cached_tokens == 2
    assert done.first_token_ms is not None


@respx.mock
async def test_fallbacks_sent_as_models_array_capped_at_two(client: OpenRouterClient) -> None:
    route = respx.post(CHAT_URL).mock(return_value=sse_response(make_stream("x")))
    await _drain(client.stream_chat(model="a/1", messages=[], fallbacks=["a/2", "a/3", "a/4"]))
    body = request_body(route.calls[0])
    assert body["model"] == "a/1"
    assert body["models"] == ["a/2", "a/3"]
    assert body["stream"] is True


@respx.mock
async def test_tool_calls_are_accumulated_across_deltas(client: OpenRouterClient) -> None:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "m/x"}

    def tc(**fields: Any) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": {"tool_calls": [fields]}}]}

    chunks = [
        tc(index=0, id="call_1", type="function", function={"name": "run_python", "arguments": ""}),
        tc(index=0, function={"arguments": '{"code": "pr'}),
        tc(index=0, function={"arguments": 'int(1)"}'}),
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    respx.post(CHAT_URL).mock(return_value=sse_response(sse(chunks)))
    events = await _drain(client.stream_chat(model="m/x", messages=[]))
    done = events[-1]
    assert isinstance(done, Completion)
    assert done.finish_reason == "tool_calls"
    assert len(done.tool_calls) == 1
    call = done.tool_calls[0]
    assert (call.id, call.name) == ("call_1", "run_python")
    assert call.parsed_arguments() == {"code": "print(1)"}
    assert done.usage is None


@respx.mock
async def test_mid_stream_error_becomes_user_error(client: OpenRouterClient) -> None:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "m/x"}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"content": "par"}}]},
        {**base, "error": {"code": 502, "message": "Provider disconnected"}, "choices": []},
    ]
    respx.post(CHAT_URL).mock(return_value=sse_response(sse(chunks)))
    with pytest.raises(OpenRouterError) as exc:
        await _drain(client.stream_chat(model="m/x", messages=[]))
    assert "Provider disconnected" in exc.value.user_message


@respx.mock
async def test_rate_limit_retries_once_then_plain_message(client: OpenRouterClient) -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            429,
            json={"error": {"code": 429, "message": "rate limited"}},
            headers={"retry-after-ms": "1"},
        )
    )
    with pytest.raises(OpenRouterError) as exc:
        await client.complete(model="m/x", messages=[])
    assert route.call_count == 2
    assert exc.value.status == 429
    assert "rate-limiting" in exc.value.user_message


@respx.mock
async def test_server_error_retries_once_then_succeeds(client: OpenRouterClient) -> None:
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(
                503, json={"error": {"message": "down"}}, headers={"retry-after-ms": "1"}
            ),
            sse_response(make_stream("fine")),
        ]
    )
    events = await _drain(client.stream_chat(model="m/x", messages=[]))
    assert route.call_count == 2
    assert isinstance(events[-1], Completion)
    assert events[-1].text == "fine"


@respx.mock
async def test_no_retry_option(client: OpenRouterClient) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(500, json={"error": {}}))
    with pytest.raises(OpenRouterError):
        await client.complete(model="m/x", messages=[], retry=False)
    assert route.call_count == 1


@respx.mock
async def test_out_of_credits_message(client: OpenRouterClient) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(402, json={"error": {"message": "x"}}))
    with pytest.raises(OpenRouterError) as exc:
        await client.complete(model="m/x", messages=[])
    assert "credits" in exc.value.user_message


@respx.mock
async def test_complete_returns_answered_model_and_cost(client: OpenRouterClient) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=json_completion("{}", model="m/real", cost=0.5))
    )
    result = await client.complete(model="m/asked", messages=[])
    assert result.model == "m/real"
    assert result.usage is not None and result.usage.cost == Decimal("0.5")


@respx.mock
async def test_mid_stream_typed_error_uses_error_type(client: OpenRouterClient) -> None:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "m/x"}
    chunks = [
        {
            **base,
            "error": {
                "code": 429,
                "message": "Rate limit exceeded",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
        }
    ]
    respx.post(CHAT_URL).mock(return_value=sse_response(sse(chunks)))
    with pytest.raises(OpenRouterError) as exc:
        await _drain(client.stream_chat(model="m/x", messages=[]))
    assert "rate-limiting" in exc.value.user_message


@respx.mock
async def test_finish_reason_error_without_error_object_fails(client: OpenRouterClient) -> None:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "m/x"}
    chunks = [{**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]}]
    respx.post(CHAT_URL).mock(return_value=sse_response(sse(chunks)))
    with pytest.raises(OpenRouterError):
        await _drain(client.stream_chat(model="m/x", messages=[]))


@respx.mock
async def test_no_zdr_provider_message_on_503(client: OpenRouterClient) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            503,
            json={"error": {"code": 503, "message": "No endpoints"}},
            headers={"retry-after-ms": "1"},
        )
    )
    with pytest.raises(OpenRouterError) as exc:
        await client.complete(model="m/x", messages=[])
    assert "zero data retention" in exc.value.user_message


@respx.mock
async def test_stream_collects_annotations(client: OpenRouterClient) -> None:
    base = {"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "m/x"}
    cite = {"type": "url_citation", "url_citation": {"url": "https://a.example", "title": "A"}}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"content": "Hi [1]"}}]},
        {
            **base,
            "choices": [{"index": 0, "delta": {"annotations": [cite]}, "finish_reason": "stop"}],
        },
    ]
    respx.post(CHAT_URL).mock(return_value=sse_response(sse(chunks)))
    events = await _drain(client.stream_chat(model="m/x", messages=[]))
    done = events[-1]
    assert isinstance(done, Completion)
    assert done.annotations == [cite]

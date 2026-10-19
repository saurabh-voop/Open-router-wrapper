"""Helpers for mocking OpenRouter with respx."""

import json
from typing import Any

import httpx

BASE_URL = "https://openrouter.test/api/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


def sse(chunks: list[dict[str, Any]], *, comments: bool = True) -> str:
    lines: list[str] = []
    if comments:
        lines.append(": OPENROUTER PROCESSING\n\n")
    lines += [f"data: {json.dumps(c)}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def text_stream(
    text: str,
    *,
    model: str = "acme/model-a",
    cost: float = 0.0012,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cached_tokens: int = 2,
) -> str:
    base = {"id": "gen-1", "object": "chat.completion.chunk", "created": 1, "model": model}
    chunks: list[dict[str, Any]] = []
    for piece in [text[i : i + 4] for i in range(0, len(text), 4)]:
        chunks.append(
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}}]}
        )
    chunks.append(
        {**base, "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}]}
    )
    # Per OpenRouter docs the usage chunk repeats the final choice with an empty delta.
    chunks.append(
        {
            **base,
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost": cost,
                "prompt_tokens_details": {"cached_tokens": cached_tokens},
            },
        }
    )
    return sse(chunks)


def sse_response(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def json_completion(content: str, *, model: str = "acme/model-a", cost: float = 0.0001) -> Any:
    return {
        "id": "gen-2",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "cost": cost},
    }


def request_body(call: Any) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(call.request.content)
    return body


def catalog_model(
    slug: str,
    *,
    context: int = 200_000,
    modalities: tuple[str, ...] = ("text",),
    params: tuple[str, ...] = ("tools", "response_format", "structured_outputs"),
    prompt: str = "0.000003",
    completion: str = "0.000015",
) -> dict[str, Any]:
    return {
        "id": slug,
        "canonical_slug": slug,
        "name": slug.split("/")[-1].title(),
        "context_length": context,
        "architecture": {"input_modalities": list(modalities), "output_modalities": ["text"]},
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": list(params),
    }


# Catalog matching tests/fixtures/routing.yaml.
FAKE_MODELS = [
    catalog_model("acme/fast-classifier", context=32_000),
    catalog_model("acme/coder"),
    catalog_model("acme/allround", modalities=("text", "image", "file")),
    catalog_model("acme/vision-pro", modalities=("text", "image")),
    catalog_model("acme/searcher"),
    catalog_model("acme/longdoc", context=1_000_000, modalities=("text", "image", "file")),
    catalog_model("acme/writer", params=()),
    catalog_model("acme/thinker", context=128_000),
    catalog_model("other/no-zdr"),
]
FAKE_ZDR = [
    {"model_id": m["id"], "provider_name": "Acme", "tag": "acme"}
    for m in FAKE_MODELS
    if m["id"] != "other/no-zdr"
]

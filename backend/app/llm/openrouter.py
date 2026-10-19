"""The only module that talks to OpenRouter (CLAUDE.md hard rule 1, SPEC section 6).

Every chat request built here carries ``provider.zdr = true`` and
``provider.data_collection = "deny"``. Callers cannot turn these off: any provider
preferences they pass are merged *under* the enforced values. ``tests/test_openrouter.py``
checks the wire body of every code path.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import httpx
import openai
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

logger = logging.getLogger(__name__)

ENFORCED_PROVIDER: dict[str, Any] = {"zdr": True, "data_collection": "deny"}
MAX_FALLBACKS = 2


def privacy_provider(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Provider preferences for a request: caller extras, then the enforced privacy values."""
    return {**(extra or {}), **ENFORCED_PROVIDER}


# ---------------------------------------------------------------- results


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: Decimal | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Usage | None:
        if not payload:
            return None
        details = payload.get("prompt_tokens_details") or {}
        cost = payload.get("cost")
        return cls(
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            cost=Decimal(str(cost)) if cost is not None else None,
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text as produced by the model

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass
class Completion:
    """Final state of one model call. ``model`` is the model that actually answered."""

    model: str
    text: str
    finish_reason: str | None
    usage: Usage | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    first_token_ms: int | None = None


StreamEvent = TextDelta | Completion


class OpenRouterError(Exception):
    """An OpenRouter failure, with a message that can be shown to the user as is."""

    def __init__(
        self, user_message: str, *, status: int | None = None, usage: Usage | None = None
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status = status
        self.usage = usage  # charges already reported before the failure, if any


# Stable ``error.metadata.error_type`` values (OpenRouter errors guide) -> user message.
_ERROR_TYPE_MESSAGES = {
    "rate_limit_exceeded": "The model providers are rate-limiting us right now. "
    "Try again in a minute.",
    "provider_overloaded": "The model provider is overloaded right now. Try again shortly.",
    "provider_unavailable": "The model provider is unavailable right now. Try again shortly.",
    "context_length_exceeded": "This conversation is too long for the model. "
    "Start a new chat or remove some attachments.",
    "content_policy_violation": "The provider refused this request under its content policy.",
    "refusal": "The model refused to answer this request.",
    "timeout": "The model took too long to respond. Please try again.",
}

_STATUS_MESSAGES = {
    401: "OpenRouter rejected the API key. Ask the admin to check it.",
    402: "The OpenRouter account is out of credits. Ask the admin to top it up.",
    403: "The request was blocked by a provider or account guardrail.",
    408: "The model took too long to respond. Please try again.",
    429: _ERROR_TYPE_MESSAGES["rate_limit_exceeded"],
    502: "The model is down or returned an invalid response. Try again shortly.",
    # 503 means no provider meets the routing requirements, which for us includes ZDR.
    503: "No provider that meets Relay's privacy requirements (zero data retention) "
    "is available for this model right now. Try again shortly or pick another model.",
}


def _error_fields(body: object) -> tuple[str, str]:
    """(message, error_type) from an OpenRouter error body or its inner ``error`` object."""
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            meta = inner.get("metadata")
            error_type = meta.get("error_type") if isinstance(meta, dict) else None
            return str(inner.get("message") or ""), str(error_type or "")
    return "", ""


def _to_user_error(err: Exception) -> OpenRouterError:
    if isinstance(err, openai.APITimeoutError):
        return OpenRouterError(_ERROR_TYPE_MESSAGES["timeout"])
    if isinstance(err, openai.APIConnectionError):
        return OpenRouterError("Could not reach OpenRouter. Please try again in a moment.")
    if isinstance(err, openai.APIStatusError):
        status = err.status_code
        detail, error_type = _error_fields(err.body)
        msg = (
            _ERROR_TYPE_MESSAGES.get(error_type)
            or _STATUS_MESSAGES.get(status)
            or (
                "The model providers are having problems right now. Try again shortly."
                if status >= 500
                else f"OpenRouter could not handle the request: {detail or err.message}"
            )
        )
        return OpenRouterError(msg, status=status)
    if isinstance(err, openai.APIError):
        # Mid-stream error: HTTP status was already 200, the error came as an SSE chunk.
        detail, error_type = _error_fields(err.body)
        msg = _ERROR_TYPE_MESSAGES.get(error_type) or (
            f"The model stopped with an error: {detail or err.message}"
        )
        return OpenRouterError(msg)
    return OpenRouterError("Unexpected error talking to OpenRouter.")


# ---------------------------------------------------------------- client


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        http_client: httpx.AsyncClient | None = None,
        app_url: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        headers = {"X-Title": "Relay"}
        if app_url:
            headers["HTTP-Referer"] = app_url
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
        # max_retries=1: one retry with backoff on 429/5xx/connection errors once the
        # OpenRouter-side fallbacks (``models``) are exhausted (SPEC section 6).
        self._sdk = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            http_client=self._http,
            max_retries=1,
            default_headers=headers,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- request body -------------------------------------------------

    @staticmethod
    def _extra_body(
        *,
        fallbacks: Sequence[str],
        tools: Sequence[dict[str, Any]] | None,
        plugins: Sequence[dict[str, Any]] | None,
        provider: dict[str, Any] | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = dict(extra or {})
        fb = [m for m in fallbacks if m][:MAX_FALLBACKS]
        if fb:
            body["models"] = fb
        if tools:
            body["tools"] = list(tools)
        if plugins:
            body["plugins"] = list(plugins)
        # Last, so nothing above (including ``extra``) can override it.
        body["provider"] = privacy_provider(provider)
        return body

    # -- chat ---------------------------------------------------------

    async def stream_chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        fallbacks: Sequence[str] = (),
        tools: Sequence[dict[str, Any]] | None = None,
        plugins: Sequence[dict[str, Any]] | None = None,
        provider: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat completion: ``TextDelta`` events, then exactly one ``Completion``."""
        started = time.monotonic()
        first_token_ms: int | None = None
        answered_model = model
        finish_reason: str | None = None
        usage: Usage | None = None
        text_parts: list[str] = []
        annotations: list[dict[str, Any]] = []
        calls: dict[int, ToolCall] = {}

        extra_body = self._extra_body(
            fallbacks=fallbacks, tools=tools, plugins=plugins, provider=provider, extra=extra
        )
        try:
            stream = cast(
                AsyncStream[ChatCompletionChunk],
                await self._sdk.chat.completions.create(
                    model=model,
                    messages=list(messages),  # type: ignore[arg-type]
                    stream=True,
                    extra_body=extra_body,
                ),
            )
            async for chunk in stream:
                data = chunk.model_dump()
                if data.get("model"):
                    answered_model = data["model"]
                if data.get("usage"):
                    usage = Usage.from_payload(data["usage"])
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    annotations.extend(delta.get("annotations") or [])
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index") or 0)
                        fn = tc.get("function") or {}
                        call = calls.setdefault(idx, ToolCall(id="", name="", arguments=""))
                        call.id = tc.get("id") or call.id
                        call.name = fn.get("name") or call.name
                        call.arguments += fn.get("arguments") or ""
                    text = delta.get("content")
                    if text:
                        if first_token_ms is None:
                            first_token_ms = int((time.monotonic() - started) * 1000)
                        text_parts.append(text)
                        yield TextDelta(text)
        except openai.OpenAIError as err:
            logger.warning("openrouter stream failed: %s", type(err).__name__)
            user_err = _to_user_error(err)
            user_err.usage = usage
            raise user_err from err

        if finish_reason == "error":
            raise OpenRouterError("The model stopped with an error. Please try again.", usage=usage)

        yield Completion(
            model=answered_model,
            text="".join(text_parts),
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=[calls[i] for i in sorted(calls)],
            annotations=annotations,
            latency_ms=int((time.monotonic() - started) * 1000),
            first_token_ms=first_token_ms,
        )

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        fallbacks: Sequence[str] = (),
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        request_timeout_s: float | None = None,
        provider: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> Completion:
        """Non-streaming completion, used by the classifier and the eval judge."""
        started = time.monotonic()
        extra_body = self._extra_body(
            fallbacks=fallbacks, tools=None, plugins=None, provider=provider, extra=None
        )
        kwargs: dict[str, Any] = {}
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        sdk = self._sdk if retry else self._sdk.with_options(max_retries=0)
        try:
            resp = cast(
                ChatCompletion,
                await sdk.chat.completions.create(
                    model=model,
                    messages=list(messages),  # type: ignore[arg-type]
                    stream=False,
                    extra_body=extra_body,
                    timeout=request_timeout_s,
                    **kwargs,
                ),
            )
        except openai.OpenAIError as err:
            logger.warning("openrouter completion failed: %s", type(err).__name__)
            raise _to_user_error(err) from err

        data = resp.model_dump()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return Completion(
            model=data.get("model") or model,
            text=message.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            usage=Usage.from_payload(data.get("usage")),
            annotations=list(message.get("annotations") or []),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # -- catalog ------------------------------------------------------

    async def _get_json(self, path: str) -> Any:
        try:
            resp = await self._http.get(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError as err:
            raise OpenRouterError(f"Could not fetch {path} from OpenRouter.") from err
        return resp.json()

    async def list_user_models(self) -> list[dict[str, Any]]:
        """Models available to this account after its provider/privacy settings."""
        payload = await self._get_json("/models/user")
        return list(payload.get("data") or [])

    async def list_zdr_endpoints(self) -> list[dict[str, Any]]:
        """Endpoints that have a zero-data-retention policy."""
        payload = await self._get_json("/endpoints/zdr")
        return list(payload.get("data") or [])

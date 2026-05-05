"""OpenAI-compatible LLM provider using the native openai SDK.

This provider supports OpenAI, DeepSeek, Doubao (ByteDance), and any other
OpenAI-compatible API endpoint.
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI
from openai import _types as _oai_types
from pydantic import BaseModel

from .base import LLMProvider
from .retry import call_with_retry
from .types import (
    ContentBlock,
    LLMMessage,
    LLMResponse,
    LLMStreamChunk,
    ModelInfo,
    ProviderInfo,
    TokenUsage,
)


def normalize_openai_base_url(base_url: str | None) -> str | None:
    """Return an SDK base URL from a user-entered OpenAI-compatible URL.

    The OpenAI SDK expects the API root, for example ``https://host/v1``.
    Users often paste the full chat-completions endpoint; if passed through
    unchanged the SDK appends ``/chat/completions`` again.
    """
    if not base_url:
        return None
    normalized = base_url.strip().rstrip("/")
    suffix = "/chat/completions"
    if normalized.lower().endswith(suffix):
        normalized = normalized[: -len(suffix)].rstrip("/")
    return normalized or None


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible provider wrapping AsyncOpenAI.

    Supports OpenAI, DeepSeek, Doubao, and any OpenAI-compatible API.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        provider_name: str = "openai",
    ) -> None:
        normalized_base_url = normalize_openai_base_url(base_url)
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=normalized_base_url,
            timeout=300.0,  # 5 minutes — research/strategy stages need long responses
        )
        self._provider_name = provider_name
        self._base_url = (normalized_base_url or "").rstrip("/")

    def _is_deepseek_request(self, model: str | None = None) -> bool:
        return (
            self._provider_name == "deepseek"
            or "api.deepseek.com" in self._base_url
            or (model or "").startswith("deepseek")
        )

    def _build_chat_kwargs(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float,
        max_tokens: int | None,
        stream: bool = False,
    ) -> dict:
        kwargs: dict = {
            "model": model,
            "messages": self._convert_messages(messages),
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if stream:
            kwargs["stream"] = True
        return kwargs

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict]:
        """Convert LLMMessage list to OpenAI message format."""
        result = []
        for msg in messages:
            if isinstance(msg.content, str):
                result.append({"role": msg.role, "content": msg.content})
            else:
                parts = []
                for block in msg.content:
                    if block.type == "text" and block.text:
                        parts.append({"type": "text", "text": block.text})
                    elif block.type == "image" and block.image_data:
                        b64 = base64.b64encode(block.image_data).decode()
                        media = block.image_media_type or "image/png"
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{b64}",
                            },
                        })
                result.append({"role": msg.role, "content": parts})
        return result

    async def chat(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_chat_kwargs(
            messages,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        t0 = time.monotonic()
        if response_format:
            resp = await call_with_retry(
                lambda: self._client.beta.chat.completions.parse(
                    **kwargs,
                    response_format=response_format,
                )
            )
        else:
            resp = await call_with_retry(
                lambda: self._client.chat.completions.create(**kwargs)
            )
        duration_ms = int((time.monotonic() - t0) * 1000)

        content = resp.choices[0].message.content or ""
        usage = None
        if resp.usage:
            usage = TokenUsage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
            )
        return LLMResponse(content=content, usage=usage, raw=resp)

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        kwargs = self._build_chat_kwargs(
            messages,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        stream = await call_with_retry(
            lambda: self._client.chat.completions.create(**kwargs)
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield LLMStreamChunk(
                    delta=delta.content,
                    finish_reason=chunk.choices[0].finish_reason,
                )

    async def validate(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    def get_provider_info(self) -> ProviderInfo:
        if self._provider_name == "deepseek":
            return ProviderInfo(
                name="deepseek",
                display_name="DeepSeek",
                default_base_url="https://api.deepseek.com",
                models=[
                    ModelInfo(
                        id="deepseek-chat",
                        display_name="DeepSeek Chat",
                        supports_vision=True,
                        supports_structured_output=True,
                        context_window=128000,
                    ),
                ],
            )
        if self._provider_name == "doubao":
            return ProviderInfo(
                name="doubao",
                display_name="Doubao (ByteDance)",
                default_base_url="https://ark.cn-beijing.volces.com/api/v3",
                models=[
                    ModelInfo(
                        id="doubao-seed-2-0-lite-260215",
                        display_name="Doubao Seed 2.0 Lite",
                        supports_vision=False,
                        supports_structured_output=False,
                        context_window=128000,
                    ),
                ],
            )
        return ProviderInfo(
            name="openai",
            display_name="OpenAI",
            models=[
                ModelInfo(
                    id="gpt-4o",
                    display_name="GPT-4o",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=128000,
                ),
                ModelInfo(
                    id="gpt-4o-mini",
                    display_name="GPT-4o Mini",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=128000,
                ),
            ],
        )

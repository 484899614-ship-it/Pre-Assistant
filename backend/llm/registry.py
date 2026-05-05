"""LLM provider registry and factory."""

from __future__ import annotations

from importlib import import_module

from .base import LLMProvider
from .types import ModelInfo, ProviderInfo

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Doubao (ByteDance) uses OpenAI-compatible API
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

_PROVIDER_IMPORTS: dict[str, tuple[str, str]] = {
    "openai": ("backend.llm.provider_openai", "OpenAIProvider"),
    "deepseek": ("backend.llm.provider_openai", "OpenAIProvider"),
    "doubao": ("backend.llm.provider_openai", "OpenAIProvider"),
}

_PROVIDER_INFO: dict[str, ProviderInfo] = {
    "openai": ProviderInfo(
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
    ),
    "deepseek": ProviderInfo(
        name="deepseek",
        display_name="DeepSeek",
        default_base_url=DEEPSEEK_BASE_URL,
        models=[
            ModelInfo(
                id="deepseek-chat",
                display_name="DeepSeek Chat",
                supports_vision=True,
                supports_structured_output=True,
                context_window=128000,
            ),
        ],
    ),
    "doubao": ProviderInfo(
        name="doubao",
        display_name="Doubao (ByteDance)",
        default_base_url=DOUBAO_BASE_URL,
        models=[
            ModelInfo(
                id="doubao-seed-2-0-lite-260215",
                display_name="Doubao Seed 2.0 Lite",
                supports_vision=False,
                supports_structured_output=False,
                context_window=128000,
            ),
        ],
    ),
}


def _load_provider_class(name: str) -> type[LLMProvider]:
    module_name, class_name = _PROVIDER_IMPORTS[name]
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or module_name
        raise RuntimeError(
            f"Provider '{name}' is unavailable because the optional dependency "
            f"'{missing}' is not installed."
        ) from exc
    return getattr(module, class_name)


def create_provider(
    name: str,
    api_key: str,
    *,
    base_url: str | None = None,
) -> LLMProvider:
    """Create an LLM provider instance by name."""
    if name not in _PROVIDER_IMPORTS:
        raise ValueError(f"Unknown provider '{name}'. Available: {list(_PROVIDER_IMPORTS)}")

    cls = _load_provider_class(name)

    resolved_base_url = base_url
    if name == "deepseek" and not resolved_base_url:
        resolved_base_url = DEEPSEEK_BASE_URL
    elif name == "doubao" and not resolved_base_url:
        resolved_base_url = DOUBAO_BASE_URL

    return cls(
        api_key=api_key,
        base_url=resolved_base_url,
        provider_name=name,
    )


def list_providers() -> list[ProviderInfo]:
    """List configured providers without importing optional SDKs."""
    return list(_PROVIDER_INFO.values())

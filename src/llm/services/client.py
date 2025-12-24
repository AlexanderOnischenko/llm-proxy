"""LLM client factory.

We keep model construction localized here so the wrapper stays provider-agnostic
and does not inherit from (or depend on) pydantic-based LangChain classes.
"""

from __future__ import annotations

from typing import Any, Dict

from llm.config.settings import Settings
from llm.services.cache import SQLiteCache
from llm.services.wrapper import CachedQueuedChatOpenAI

def create_custom_llm(
    settings: Settings,
    cache: SQLiteCache | None = None,
    **override_kwargs: Any
) -> CachedQueuedChatOpenAI:
    """
    Создаёт экземпляр CachedQueuedChatOpenAI на основании переданных Settings
    и любых override_kwargs.
    """
    priority_total_slots = max(settings.llm_priority_total_slots, 1)

    model_params: Dict[str, Any] = {
        "base_url": str(settings.llm_api_base),
        # LangChain OpenAI integration uses `model` as the canonical name.
        "model": settings.llm_model_name,
        "temperature": settings.llm_temperature,
        "api_key": settings.llm_api_key,
        "verbose": settings.llm_verbose,
        # Keep backwards compatible name; some model families prefer `max_completion_tokens`.
        "max_tokens": settings.llm_max_tokens,
    }
    wrapper_params: Dict[str, Any] = {
        "cache_enabled": settings.llm_cache_enabled,
        "cache_path": settings.llm_cache_path,
        "cache_ttl": settings.llm_cache_ttl,
        "max_concurrent": max(settings.llm_max_concurrent, priority_total_slots),
    }

    wrapper_override_keys = {
        "cache_enabled",
        "cache_path",
        "cache_ttl",
        "max_concurrent",
        "bp_cache",
        "cache",
    }

    for key in list(override_kwargs.keys()):
        if key in wrapper_override_keys:
            wrapper_params[key] = override_kwargs.pop(key)

    model_params.update(override_kwargs)

    # Local import: tests and some minimal runtimes may not have LC installed.
    try:
        from langchain_openai import ChatOpenAI  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "langchain_openai is required to build the default ChatOpenAI model"
        ) from exc

    model = ChatOpenAI(**model_params)
    if cache is not None:
        wrapper_params["bp_cache"] = cache
    return CachedQueuedChatOpenAI(model=model, **wrapper_params)

"""LLM client factory.

We keep model construction localized here so the wrapper stays provider-agnostic
and does not inherit from (or depend on) pydantic-based LangChain classes.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import SecretStr

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

    # langchain-openai types `api_key` as SecretStr | None.
    api_key = SecretStr(settings.llm_api_key) if settings.llm_api_key else None

    # langchain-openai's type stubs don't always expose `max_tokens` as a top-level
    # kwarg. Passing it via model_kwargs keeps both runtime behavior and static typing.
    model_kwargs: Dict[str, Any] = {"max_tokens": settings.llm_max_tokens}

    model_params: Dict[str, Any] = {
        "base_url": str(settings.llm_api_base),
        # LangChain OpenAI integration uses `model` as the canonical name.
        "model": settings.llm_model_name,
        "temperature": settings.llm_temperature,
        "api_key": api_key,
        "verbose": settings.llm_verbose,
        "model_kwargs": model_kwargs,
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

    # Allow callers to override legacy `max_tokens` without passing it as a top-level kwarg.
    if "max_tokens" in override_kwargs:
        model_params.setdefault("model_kwargs", {})
        model_params["model_kwargs"]["max_tokens"] = override_kwargs.pop("max_tokens")

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

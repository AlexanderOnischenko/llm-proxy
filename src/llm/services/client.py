"""Factory for creating cached async chat models."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Type

from langchain_core.language_models.chat_models import BaseChatModel

from shared_pkg.config.settings import Settings
from llm.services.gigachat_wrapper import CachedQueuedGigaChat
from llm.services.wrapper import CachedQueuedChatOpenAI


class LLMProvider(str, Enum):
    OPENAI = "openai"
    GIGACHAT = "gigachat"

    @classmethod
    def from_settings(cls, value: str | None) -> "LLMProvider":
        try:
            return cls((value or cls.OPENAI.value).lower())
        except ValueError as exc:  # pragma: no cover - configuration bug
            raise ValueError(f"Unsupported LLM provider: {value}") from exc


ProviderClass = Type[BaseChatModel]


def _common_params(settings: Settings) -> Dict[str, Any]:
    return {
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "verbose": settings.llm_verbose,
        "cache_enabled": settings.llm_cache_enabled,
        "cache_path": settings.llm_cache_path,
        "cache_ttl": settings.llm_cache_ttl,
        "max_concurrent": settings.llm_max_concurrent,
    }


def _build_openai_params(settings: Settings) -> Dict[str, Any]:
    params = _common_params(settings)
    params.update(
        {
            "base_url": str(settings.llm_api_base),
            "model_name": settings.llm_model_name,
            "model": settings.llm_model_name,
            "api_key": settings.llm_api_key,
        }
    )
    return params


def _build_gigachat_params(settings: Settings) -> Dict[str, Any]:
    params = _common_params(settings)
    model = settings.gigachat_model or settings.llm_model_name
    params.update(
        {
            "model": model,
            "timeout": settings.llm_timeout,
            "verify_ssl_certs": settings.gigachat_verify_ssl,
        }
    )

    if settings.gigachat_url is not None:
        params["base_url"] = str(settings.gigachat_url)
    if settings.gigachat_auth_url is not None:
        params["auth_url"] = str(settings.gigachat_auth_url)
    if settings.gigachat_auth_key:
        params["credentials"] = settings.gigachat_auth_key
    if settings.gigachat_scope:
        params["scope"] = settings.gigachat_scope
    if settings.gigachat_ca_bundle:
        params["ca_bundle_file"] = settings.gigachat_ca_bundle

    missing: list[str] = []
    if "auth_url" not in params:
        missing.append("GIGACHAT_AUTH_URL")
    if "credentials" not in params:
        missing.append("GIGACHAT_AUTH_KEY")
    if missing:
        raise ValueError(
            "Missing required GigaChat configuration values: " + ", ".join(missing)
        )

    return params


def create_custom_llm(settings: Settings, **override_kwargs: Any) -> BaseChatModel:
    """Create an async-only cached chat model instance according to Settings."""

    provider = LLMProvider.from_settings(settings.llm_provider)
    if provider is LLMProvider.GIGACHAT:
        params = _build_gigachat_params(settings)
        llm_cls: ProviderClass = CachedQueuedGigaChat
    else:
        params = _build_openai_params(settings)
        llm_cls = CachedQueuedChatOpenAI

    params.update(override_kwargs)
    return llm_cls(**params)


__all__ = ["LLMProvider", "create_custom_llm"]

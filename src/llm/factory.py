from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from llm.config.loader import load_llm_config, pick_active
from llm.config.schema import GoogleGeminiCfg, GigaChatCfg, OpenAICompatCfg
from llm.services.gigachat_wrapper import CachedQueuedGigaChat
from llm.services.wrapper import CachedQueuedChatOpenAI


logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("/home/appuser/app/llm_providers.yaml")
REPO_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "llm_providers.yaml"


def _determine_config_path(explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"LLM provider configuration not found at '{path}'."
                " Set LLM_PROVIDERS_CONFIG or provide a valid config_path."
            )
        return path

    env_path = os.getenv("LLM_PROVIDERS_CONFIG")
    if env_path:
        path = Path(env_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"LLM provider configuration not found at '{path}'."
                " Update LLM_PROVIDERS_CONFIG to point to an existing file."
            )
        return path

    for candidate in (DEFAULT_CONFIG_PATH, REPO_CONFIG_PATH):
        if candidate.is_file():
            return candidate

    searched = ", ".join(str(p) for p in (DEFAULT_CONFIG_PATH, REPO_CONFIG_PATH))
    raise FileNotFoundError(
        "Unable to locate an LLM provider configuration file."
        f" Checked the following locations: {searched}."
        " Provide config_path or set LLM_PROVIDERS_CONFIG to override."
    )


def create_llm_from_config(config_path: str | None = None, **override: Any):
    path = _determine_config_path(config_path)
    cfg = load_llm_config(str(path))
    active_name, provider = pick_active(cfg)
    defaults = cfg.llm.defaults

    sanitized = provider.model_dump()
    for sensitive_key in ("credentials", "api_key"):
        if sensitive_key in sanitized:
            sanitized[sensitive_key] = "<hidden>"
    logger.info(
        "Using LLM provider '%s' (%s) with parameters: %s",
        active_name,
        provider.type,
        sanitized,
    )

    common = dict(
        temperature=override.get("temperature", defaults.temperature),
        max_tokens=override.get("max_tokens", defaults.max_tokens),
        cache_enabled=override.get("cache_enabled", defaults.cache.enabled),
        cache_path=override.get("cache_path", defaults.cache.path),
        cache_ttl=override.get("cache_ttl", defaults.cache.ttl),
        max_concurrent=override.get("max_concurrent", defaults.cache.max_concurrent),
        timeout=override.get("timeout", defaults.timeout),
        verbose=override.get("verbose", defaults.verbose),
    )

    if isinstance(provider, GigaChatCfg):
        params = dict(
            model=provider.model or override.get("model"),
            base_url=str(provider.url),
            auth_url=str(provider.auth_url),
            credentials=provider.credentials,
            scope=provider.scope,
            verify_ssl_certs=provider.verify_ssl,
        )
        if provider.verify_ssl and provider.ca_bundle:
            params["ca_bundle_file"] = provider.ca_bundle
        params.update(common)
        return CachedQueuedGigaChat(**params)

    if isinstance(provider, OpenAICompatCfg):
        params = dict(
            base_url=str(provider.base_url),
            api_key=provider.api_key,
            model=provider.model or override.get("model"),
        )
        params.update(common)
        return CachedQueuedChatOpenAI(**params)

    if isinstance(provider, GoogleGeminiCfg):
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_name = provider.model or override.get("model")
        return ChatGoogleGenerativeAI(
            api_key=provider.api_key,
            model=model_name or "gemini-1.5-pro",
            temperature=common["temperature"],
            max_output_tokens=common["max_tokens"],
        )

    raise RuntimeError(f"Provider type not supported: {provider.type}")

# services/llm/client.py
from typing import Any, Dict

from llm.config.settings import Settings
from llm.services.wrapper import CachedQueuedChatOpenAI
from llm.services.cache import SQLiteCache

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
    params: Dict[str, Any] = {
        # ChatOpenAI параметры
        "base_url":        str(settings.llm_api_base),
        "model_name":       settings.llm_model_name,
        "model":            settings.llm_model_name,
        "temperature":     settings.llm_temperature,
        "api_key":  settings.llm_api_key,
        "verbose":         settings.llm_verbose,
        "max_tokens":      settings.llm_max_tokens,
        # кеш и очередь
        "cache_enabled":   settings.llm_cache_enabled,
        "cache_path":      settings.llm_cache_path,
        "cache_ttl":       settings.llm_cache_ttl,
        "max_concurrent":  max(settings.llm_max_concurrent, priority_total_slots),
    }

    params.update(override_kwargs)
    if cache is not None:
        params["cache"] = cache
    return CachedQueuedChatOpenAI(**params)

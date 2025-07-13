# src/deps.py
from functools import lru_cache
from shared_pkg.config.settings import get_settings, Settings
from fastapi import Depends
from llm.services.client import create_custom_llm, CachedQueuedChatOpenAI

_llm_instance: CachedQueuedChatOpenAI | None = None

def build_llm(settings: Settings) -> CachedQueuedChatOpenAI:
    """
    Internal singleton builder for your LLM client.
    Можно вызывать напрямую из кода (например, в стартапе),
    или из DI-функции ниже.
    """
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = create_custom_llm(settings)
    return _llm_instance

def get_llm(settings: Settings = Depends(get_settings)) -> CachedQueuedChatOpenAI:
    """
    FastAPI dependency: возвращает лбелый singleton LLM-клиента.
    """
    return build_llm(settings)


# src/deps.py
from fastapi import Depends
from langchain_core.language_models.chat_models import BaseChatModel

from shared_pkg.config.settings import Settings, get_settings

from llm.factory import create_llm_from_config

_llm_instance: BaseChatModel | None = None

def build_llm(settings: Settings | None = None) -> BaseChatModel:
    """
    Internal singleton builder for your LLM client.
    Можно вызывать напрямую из кода (например, в стартапе),
    или из DI-функции ниже.
    """
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = create_llm_from_config()
    return _llm_instance

def get_llm(settings: Settings = Depends(get_settings)) -> BaseChatModel:
    """
    FastAPI dependency: возвращает лбелый singleton LLM-клиента.
    """
    _ = settings  # trigger dependency resolution, keep backwards compatibility
    return build_llm()


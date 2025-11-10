# src/deps.py
from shared_pkg.config.settings import get_settings, Settings
from fastapi import Depends
from langchain_core.language_models.chat_models import BaseChatModel
from llm.services.client import create_custom_llm

_llm_instance: BaseChatModel | None = None

def build_llm(settings: Settings) -> BaseChatModel:
    """
    Internal singleton builder for your LLM client.
    Можно вызывать напрямую из кода (например, в стартапе),
    или из DI-функции ниже.
    """
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = create_custom_llm(settings)
    return _llm_instance

def get_llm(settings: Settings = Depends(get_settings)) -> BaseChatModel:
    """
    FastAPI dependency: возвращает лбелый singleton LLM-клиента.
    """
    return build_llm(settings)


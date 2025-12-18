# src/deps.py
from shared_pkg.config.settings import Settings, get_settings
from fastapi import Depends
from llm.services.client import CachedQueuedChatOpenAI, create_custom_llm
from llm.services.chat_provider import OpenAIChatProvider
from llm.services.priority_queue import PriorityChatQueue

_llm_instance: CachedQueuedChatOpenAI | None = None
_chat_provider: OpenAIChatProvider | None = None
_chat_queue: PriorityChatQueue | None = None

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


def build_chat_provider(settings: Settings) -> OpenAIChatProvider:
    global _chat_provider
    if _chat_provider is None:
        _chat_provider = OpenAIChatProvider(settings)
    return _chat_provider


def get_chat_provider(settings: Settings = Depends(get_settings)) -> OpenAIChatProvider:
    return build_chat_provider(settings)


def build_chat_queue(
    settings: Settings, provider: OpenAIChatProvider
) -> PriorityChatQueue:
    global _chat_queue
    if _chat_queue is None:
        _chat_queue = PriorityChatQueue(
            handler=provider.complete,
            limits=settings.llm_priority_limits_map,
            max_queue=settings.llm_queue_max_len,
            default_priority=settings.llm_priority_default,
        )
    return _chat_queue


def get_chat_queue(
    settings: Settings = Depends(get_settings),
    provider: OpenAIChatProvider = Depends(get_chat_provider),
) -> PriorityChatQueue:
    return build_chat_queue(settings, provider)


def reset_chat_dependencies() -> None:
    global _chat_queue, _chat_provider
    _chat_queue = None
    _chat_provider = None

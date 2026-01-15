# src/deps.py
from fastapi import Depends

from llm.config.settings import Settings, get_settings
from llm.services.cache import SQLiteCache
from llm.services.chat_provider import OpenAIChatProvider
from llm.services.client import CachedQueuedChatOpenAI, create_custom_llm
from llm.services.priority_queue import PriorityChatQueue
from llm.services.provider_protocol import ChatProvider
from llm.services.transport import HttpClientFactory

_llm_instance: CachedQueuedChatOpenAI | None = None
_chat_provider: ChatProvider | None = None
_chat_queue: PriorityChatQueue | None = None
_shared_cache: SQLiteCache | None = None
_http_client_factory: HttpClientFactory | None = None


def build_cache(settings: Settings) -> SQLiteCache:
    """Internal singleton builder for shared SQLite cache."""
    global _shared_cache
    if _shared_cache is None:
        cache_path = getattr(settings, "llm_cache_path_resolved", None) or settings.llm_cache_path
        _shared_cache = SQLiteCache(
            path=str(cache_path),
            ttl_seconds=settings.llm_cache_ttl,
            enabled=settings.llm_cache_enabled,
        )
    return _shared_cache


def get_cache(settings: Settings = Depends(get_settings)) -> SQLiteCache:
    return build_cache(settings)

def build_llm(settings: Settings) -> CachedQueuedChatOpenAI:
    """
    Internal singleton builder for your LLM client.
    Можно вызывать напрямую из кода (например, в стартапе),
    или из DI-функции ниже.
    """
    global _llm_instance
    if _llm_instance is None:
        transport_factory = build_http_client_factory(settings)
        provider_name = OpenAIChatProvider.provider_name
        transport_choice = transport_factory.resolve_policy(provider_name)
        _llm_instance = create_custom_llm(
            settings,
            cache=build_cache(settings),
            http_async_client=transport_factory.get_async_client(transport_choice),
            http_client=transport_factory.get_client(transport_choice),
        )
    return _llm_instance

def get_llm(settings: Settings = Depends(get_settings)) -> CachedQueuedChatOpenAI:
    """
    FastAPI dependency: возвращает лбелый singleton LLM-клиента.
    """
    return build_llm(settings)


def build_chat_provider(settings: Settings) -> ChatProvider:
    global _chat_provider
    if _chat_provider is None:
        _chat_provider = OpenAIChatProvider(
            settings,
            cache=build_cache(settings),
            transport_factory=build_http_client_factory(settings),
        )
    return _chat_provider


def build_http_client_factory(settings: Settings) -> HttpClientFactory:
    global _http_client_factory
    if _http_client_factory is None:
        _http_client_factory = HttpClientFactory(settings)
    return _http_client_factory


def get_http_client_factory(settings: Settings = Depends(get_settings)) -> HttpClientFactory:
    return build_http_client_factory(settings)


def get_chat_provider(settings: Settings = Depends(get_settings)) -> ChatProvider:
    return build_chat_provider(settings)


def build_chat_queue(
    settings: Settings, provider: ChatProvider
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
    provider: ChatProvider = Depends(get_chat_provider),
) -> PriorityChatQueue:
    return build_chat_queue(settings, provider)


def reset_chat_dependencies() -> None:
    global _chat_queue, _chat_provider, _llm_instance, _shared_cache, _http_client_factory
    _chat_queue = None
    _chat_provider = None
    _llm_instance = None
    if _shared_cache is not None:
        _shared_cache.close()
    _shared_cache = None
    if _http_client_factory is not None:
        _http_client_factory.close()
    _http_client_factory = None

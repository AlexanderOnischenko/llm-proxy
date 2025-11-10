"""GigaChat client with shared async cache/concurrency controls."""
from __future__ import annotations

from langchain_gigachat import GigaChat

from .wrapper import AsyncCachedLLMMixin


class CachedQueuedGigaChat(AsyncCachedLLMMixin, GigaChat):
    """LangChain GigaChat model with sqlite cache + concurrency guard."""

    pass


__all__ = ["CachedQueuedGigaChat"]

"""Common async caching wrapper for LangChain chat models."""
from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from langchain_core.callbacks.stdout import StdOutCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult, LLMResult
from langchain_core.prompt_values import PromptValue
from langchain_openai import ChatOpenAI
from pydantic import ConfigDict

from shared_pkg.utils.logging_config import get_logger

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────
# Silence verbose on_chat_model_start spam in logs
# ───────────────────────────────────────────────────────────
def _no_op_on_chat_model_start(self, serialized: dict, messages: list[Any], **kwargs):
    logger.info("[LLM START] model=%s  messages=%s", serialized.get("model_name"), len(messages))
    return None


StdOutCallbackHandler.on_chat_model_start = _no_op_on_chat_model_start


class AsyncCachedLLMMixin:
    """Reusable mixin that adds caching & concurrency limits to chat models."""

    model_config = ConfigDict(extra="ignore")

    def __init__(self, **kwargs: Any) -> None:
        cache_enabled = kwargs.pop("cache_enabled", True)
        cache_path = kwargs.pop("cache_path", ":memory:")
        cache_ttl = kwargs.pop("cache_ttl", 0)
        max_concurrent = kwargs.pop("max_concurrent", 4)

        super().__init__(**kwargs)  # type: ignore[misc]

        object.__setattr__(self, "cache_enabled", cache_enabled)
        object.__setattr__(self, "cache_path", cache_path)
        object.__setattr__(self, "cache_ttl", cache_ttl)

        self._sem = asyncio.Semaphore(max_concurrent)
        self._inflight: Dict[str, asyncio.Future] = {}

        self._cache_lock: threading.Lock | None = None
        self._conn: sqlite3.Connection | None = None
        if self.cache_enabled:
            self._cache_lock = threading.Lock()
            self._conn = sqlite3.connect(self.cache_path, check_same_thread=False, timeout=30)
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cache(" "  key TEXT PRIMARY KEY, value BLOB, ts INTEGER" ")"
            )
            if self.cache_ttl > 0:
                cutoff = int(time.time()) - self.cache_ttl
                cur.execute("DELETE FROM cache WHERE ts < ?", (cutoff,))
            self._conn.commit()

    # -------------- utilities --------------

    @contextmanager
    def no_cache(self):
        prev = self.cache_enabled
        object.__setattr__(self, "cache_enabled", False)
        try:
            yield
        finally:
            object.__setattr__(self, "cache_enabled", prev)

    def _make_key(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        blob = json.dumps({"method": method, "args": args, "kwargs": kwargs}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _get_cached(self, key: str):
        if not self.cache_enabled or not self._conn or not self._cache_lock:
            return None
        with self._cache_lock:
            row = self._conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        val, ts = row
        if self.cache_ttl > 0 and time.time() - ts > self.cache_ttl:
            with self._cache_lock:
                self._conn.execute("DELETE FROM cache WHERE key=?", (key,))
                self._conn.commit()
            logger.info("[CACHE EXPIRED] %s", key)
            return None
        return pickle.loads(val)

    def _set_cached(self, key: str, value: Any):
        if not self.cache_enabled or not self._conn or not self._cache_lock:
            return
        blob = pickle.dumps(value)
        with self._cache_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache(key, value, ts) VALUES(?, ?, ?)",
                (key, blob, int(time.time())),
            )
            self._conn.commit()

    def _normalize_messages(self, messages: List[Any]) -> List[BaseMessage]:
        norm: List[BaseMessage] = []
        for m in messages:
            if isinstance(m, BaseMessage):
                norm.append(m)
            elif isinstance(m, dict):
                t = m.get("type")
                c = m.get("content", "")
                if t == "system":
                    norm.append(SystemMessage(content=c))
                elif t in ("user", "human"):
                    norm.append(HumanMessage(content=c))
                elif t in ("assistant", "ai"):
                    norm.append(AIMessage(content=c))
                else:
                    raise ValueError(f"Unknown message type: {t}")
            else:
                raise ValueError(f"Unsupported message element: {m!r}")
        return norm

    @staticmethod
    def _message_dump(message: BaseMessage) -> Dict[str, Any]:
        """Return a stable dictionary representation of a message."""
        if hasattr(message, "model_dump"):
            return message.model_dump()
        if hasattr(message, "dict"):
            return message.dict()
        if hasattr(message, "__dict__"):
            return dict(message.__dict__)
        raise TypeError(f"Unsupported message type for dumping: {type(message)!r}")

    async def _run_queued(self, key: str, coro_factory: Callable[[], Awaitable[Any]]):
        """In-flight dedup + concurrency limiting wrapper for async calls."""
        existing = self._inflight.get(key)
        if existing:
            return await existing
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            async with self._sem:
                result = await coro_factory()
                fut.set_result(result)
                return result
        except Exception as exc:  # pragma: no cover - propagate to callers
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

    # -------------- async public API --------------

    async def apredict(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> str:
        method = "apredict"
        key = self._make_key(method, (prompt,), {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] apredict, key = %s", key)
                return cached
        logger.info("[CACHE MISS] apredict, key = %s", key)

        parent = super()

        async def _call():
            return await parent.apredict(prompt, stop=stop)

        out = await self._run_queued(key, _call)
        self._set_cached(key, out)
        return out

    async def acomplete(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        method = "acomplete"
        key = self._make_key(method, (prompt,), {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] acomplete, key = %s", key)
                return cached
        logger.info("[CACHE MISS] acomplete, key = %s", key)

        parent = super()

        async def _call():
            return await parent.acomplete(prompt, stop=stop)

        out = await self._run_queued(key, _call)
        self._set_cached(key, out)
        return out

    async def abatch(self, prompts: List[str], stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        method = "abatch"
        key = self._make_key(method, (tuple(prompts),), {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] abatch, key = %s", key)
                return cached
        logger.info("[CACHE MISS] abatch, key = %s", key)

        parent = super()

        async def _call():
            return await parent.abatch(prompts, stop=stop)

        out = await self._run_queued(key, _call)
        self._set_cached(key, out)
        return out

    async def agenerate(
        self,
        messages: List[Union[BaseMessage, dict]],
        stop: Optional[List[str]] = None,
        callbacks: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_manager: Any = None,
        sender: Optional[str] = None,
    ) -> ChatResult:
        method = "agenerate"
        msgs = self._normalize_messages(messages)
        msgs_for_cache = (tuple(self._message_dump(m) for m in msgs),)
        key = self._make_key(method, msgs_for_cache, {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] agenerate, key = %s", key)
                return cached
        logger.info("[CACHE MISS] agenerate, key = %s", key)

        parent = super()

        async def _call():
            return await parent.agenerate(
                messages=[msgs],
                stop=stop,
                callbacks=callbacks,
                tags=tags,
                metadata=metadata,
            )

        out = await self._run_queued(key, _call)
        self._set_cached(key, out)
        return out

    async def ainvoke(
        self,
        input: Optional[Union[PromptValue, str]] = None,
        /,
        *,
        messages: Optional[List[Union[BaseMessage, dict]]] = None,
        stop: Optional[List[str]] = None,
        callbacks: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_manager: Any = None,
        sender: Optional[str] = None,
    ) -> ChatResult:
        method = "ainvoke"
        if messages is not None:
            msgs = self._normalize_messages(messages)
            key_args: tuple[Any, ...] = (tuple(self._message_dump(m) for m in msgs),)
        else:
            key_args = (input,)
        key = self._make_key(method, key_args, {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] ainvoke, key = %s", key)
                return cached
        logger.info("[CACHE MISS] ainvoke, key = %s", key)

        parent = super()

        async def _call():
            return await parent.ainvoke(
                input,
                messages=messages,
                stop=stop,
                callbacks=callbacks,
                tags=tags,
                metadata=metadata,
            )

        out = await self._run_queued(key, _call)
        self._set_cached(key, out)
        return out

    # -------------- disable sync API --------------
    def _no_sync_guard(self, *args: Any, **kwargs: Any):
        raise RuntimeError("Sync методы отключены. Используйте a*-версии (async).")

    predict = _no_sync_guard
    complete = _no_sync_guard
    batch = _no_sync_guard
    generate = _no_sync_guard
    invoke = _no_sync_guard
    stream = _no_sync_guard


class CachedQueuedChatOpenAI(AsyncCachedLLMMixin, ChatOpenAI):
    """ChatOpenAI with async cache + concurrency controls."""

    pass


__all__ = ["AsyncCachedLLMMixin", "CachedQueuedChatOpenAI"]

# llm/services/wrapper.py
import time
import threading
import sqlite3
import json
import hashlib
import pickle
import asyncio
from pydantic import ConfigDict
from typing import List, Optional, Any, Dict, Union
from contextlib import contextmanager

from langchain_core.callbacks.stdout import StdOutCallbackHandler
from langchain_openai import ChatOpenAI
from langchain.schema import (
    HumanMessage,
    SystemMessage,
    AIMessage,
    BaseMessage,
    LLMResult,
    ChatResult,
    PromptValue,
)

from shared_pkg.utils.logging_config import get_logger

logger = get_logger(__name__)

# ───────────────────────────────────────────────────────────
# Silence verbose on_chat_model_start spam in logs
# ───────────────────────────────────────────────────────────
def _no_op_on_chat_model_start(self, serialized: dict, messages: list[Any], **kwargs):
    logger.info(f"[LLM START] model={serialized.get('model_name')}  messages={len(messages)}")
    return None
StdOutCallbackHandler.on_chat_model_start = _no_op_on_chat_model_start


class CachedQueuedChatOpenAI(ChatOpenAI):
    """
    ChatOpenAI with:
      - SQLite cache (optional)
      - Async concurrency limiter (asyncio.Semaphore)
      - In-flight deduplication for identical requests
      - No thread executors; all calls use native async methods
    """
    model_config = ConfigDict(extra="ignore")

    def __init__(self, **kwargs):
        # Custom params
        cache_enabled  = kwargs.pop("cache_enabled", True)
        cache_path     = kwargs.pop("cache_path", ":memory:")
        cache_ttl      = kwargs.pop("cache_ttl", 0)
        max_concurrent = kwargs.pop("max_concurrent", 4)
        # Init parent (ChatOpenAI)
        super().__init__(**kwargs)

        # Store custom params
        object.__setattr__(self, "cache_enabled",  cache_enabled)
        object.__setattr__(self, "cache_path",     cache_path)
        object.__setattr__(self, "cache_ttl",      cache_ttl)

        # Async queue & inflight map
        self._sem = asyncio.Semaphore(max_concurrent)
        self._inflight: Dict[str, asyncio.Future] = {}

        # Optional SQLite cache
        if self.cache_enabled:
            self._cache_lock = threading.Lock()
            # timeout + WAL to avoid writer stalls
            self._conn = sqlite3.connect(self.cache_path, check_same_thread=False, timeout=30)
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cache("
                "  key TEXT PRIMARY KEY, value BLOB, ts INTEGER"
                ")"
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

    def _make_key(self, method: str, args: tuple, kwargs: dict) -> str:
        blob = json.dumps({"method": method, "args": args, "kwargs": kwargs},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _get_cached(self, key: str):
        with self._cache_lock:
            row = self._conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        val, ts = row
        if self.cache_ttl > 0 and time.time() - ts > self.cache_ttl:
            with self._cache_lock:
                self._conn.execute("DELETE FROM cache WHERE key=?", (key,))
                self._conn.commit()
            logger.info(f"[CACHE EXPIRED] {key}")
            return None
        return pickle.loads(val)

    def _set_cached(self, key: str, value: Any):
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

    async def _run_queued(self, key: str, coro_factory):
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
        except Exception as e:
            fut.set_exception(e)
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
                logger.info(f"[CACHE HIT] apredict, key = {key}")
                return cached
        logger.info(f"[CACHE MISS] apredict, key = {key}")
        async def _call():
            return await super(CachedQueuedChatOpenAI, self).apredict(prompt, stop=stop)
        out = await self._run_queued(key, _call)
        if self.cache_enabled:
            self._set_cached(key, out)
        return out

    async def acomplete(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        method = "acomplete"
        key = self._make_key(method, (prompt,), {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info(f"[CACHE HIT] acomplete, key = {key}")
                return cached
        logger.info(f"[CACHE MISS] acomplete, key = {key}")
        async def _call():
            return await super(CachedQueuedChatOpenAI, self).acomplete(prompt, stop=stop)
        out = await self._run_queued(key, _call)
        if self.cache_enabled:
            self._set_cached(key, out)
        return out

    async def abatch(self, prompts: List[str], stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        method = "abatch"
        key = self._make_key(method, (tuple(prompts),), {"stop": stop, "sender": sender})
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info(f"[CACHE HIT] abatch, key = {key}")
                return cached
        logger.info(f"[CACHE MISS] abatch, key = {key}")
        async def _call():
            return await super(CachedQueuedChatOpenAI, self).abatch(prompts, stop=stop)
        out = await self._run_queued(key, _call)
        if self.cache_enabled:
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
        msgs = [msgs]
        key = self._make_key(
            method,
            (tuple(self._message_dump(m) for m in msgs[0]),),
            {"stop": stop, "sender": sender},
        )
        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info(f"[CACHE HIT] agenerate, key = {key}")
                return cached
        logger.info(f"[CACHE MISS] agenerate, key = {key}")
        async def _call():
            return await super(CachedQueuedChatOpenAI, self).agenerate(
                messages=msgs,
                stop=stop,
                callbacks=callbacks,
                tags=tags,
                metadata=metadata,
                # run_manager опускаем — в текущей версии LC его уже прокидывают внутри
            )
        out = await self._run_queued(key, _call)
        if self.cache_enabled:
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
        key_args: tuple[Any, ...]
        if messages is not None:
            msgs = self._normalize_messages(messages)
            key_args = (tuple(self._message_dump(m) for m in msgs),)
        else:
            key_args = (input,)
        key = self._make_key(method, key_args, {"stop": stop, "sender": sender})

        if self.cache_enabled:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info(f"[CACHE HIT] ainvoke, key = {key}")
                return cached
        logger.info(f"[CACHE MISS] ainvoke, key = {key}")
        async def _call():
            return await super(CachedQueuedChatOpenAI, self).ainvoke(
                input,
                messages=messages,
                stop=stop,
                callbacks=callbacks,
                tags=tags,
                metadata=metadata,
            )
        out = await self._run_queued(key, _call)
        if self.cache_enabled:
            self._set_cached(key, out)
        return out

    # -------------- disable sync API --------------
    def _no_sync_guard(self, *args, **kwargs):
        raise RuntimeError("Sync методы отключены. Используйте a*-версии (async).")

    # Disable sync methods explicitly
    predict = _no_sync_guard
    complete = _no_sync_guard
    batch = _no_sync_guard
    generate = _no_sync_guard
    invoke = _no_sync_guard
    stream = _no_sync_guard

import time
import threading
import sqlite3
import json
import hashlib
import pickle
import asyncio
import inspect
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Optional, AsyncIterator, Iterator, Any, Dict, Union

from langchain_core.callbacks.stdout import StdOutCallbackHandler
from langchain_community.chat_models import ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage, AIMessage, BaseMessage
from langchain.schema import LLMResult, ChatResult, PromptValue
from utils.logger import logger

# ===========================
# Вспомогательные функции и настройка логирования
# ===========================

def flatten_list(nested_list):
    flat_list = []
    for item in nested_list:
        if isinstance(item, (list, tuple)):
            flat_list.extend(flatten_list(item))
        else:
            flat_list.append(item)
    return flat_list

# Переопределяем on_chat_model_start в StdOutCallbackHandler
def _no_op_on_chat_model_start(self, serialized: dict, messages: list[Any], **kwargs):
    print(f"[LLM START] model={serialized.get('model_name')}  messages={len(messages)}")
    return None
StdOutCallbackHandler.on_chat_model_start = _no_op_on_chat_model_start

# ===========================
# Конфигурация
# ===========================
MAX_CONCURRENT = 1
CACHE_ENABLED = True
CACHE_PATH    = "llm_cache.sqlite"
CACHE_TTL     = 600000



# ===========================
# Менеджер потоков через ThreadPoolExecutor
# ===========================
class _QueueManager:
    def __init__(self, real_llm: ChatOpenAI):
        self._llm = real_llm
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)

    def submit(self, method: str, *args, **kwargs) -> Future:
        # Фильтруем kwargs по сигнатуре
        fn = getattr(self._llm, method)
        sig = inspect.signature(fn)
        safe_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

        # Специальная логика для generate
        call_fn = fn
        call_args = args
        if method == "generate":
            call_args = ([flatten_list(args)],)
            call_fn = self._llm.generate

        logger.debug(f"Submitting {method} with args={call_args} kwargs={safe_kwargs}")
        return self._executor.submit(call_fn, *call_args, **safe_kwargs)

    async def submit_async(self, method: str, *args, **kwargs) -> Any:
        future = self.submit(method, *args, **kwargs)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

# ===========================
# Класс LLM с кэшированием и очередью
# ===========================
class CachedQueuedChatOpenAI(ChatOpenAI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        real = ChatOpenAI(**kwargs)
        self._qm = _QueueManager(real)

        if CACHE_ENABLED:
            self._cache_lock = threading.Lock()
            self._conn = sqlite3.connect(CACHE_PATH, check_same_thread=False)
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cache(
                  key   TEXT PRIMARY KEY,
                  value BLOB,
                  ts    INTEGER
                )
                """
            )
            if CACHE_TTL > 0:
                cutoff = int(time.time()) - CACHE_TTL
                cur.execute("DELETE FROM cache WHERE ts < ?", (cutoff,))
            self._conn.commit()

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
        if CACHE_TTL > 0 and time.time() - ts > CACHE_TTL:
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

    def _normalize_messages(self, messages: list[Any]) -> list[BaseMessage]:
        norm: list[BaseMessage] = []
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

    def predict(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> str:
        method = "predict"
        key = self._make_key(method, (prompt,), {"stop": stop, "sender": sender})
        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] predict")
                return cached
        future = self._qm.submit(method, prompt, stop=stop, sender=sender)
        out = future.result()
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    def complete(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        method = "complete"
        key = self._make_key(method, (prompt,), {"stop": stop, "sender": sender})
        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] complete")
                return cached
        future = self._qm.submit(method, prompt, stop=stop, sender=sender)
        out = future.result()
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    def batch(self, prompts: List[str], stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        method = "batch"
        key = self._make_key(method, (tuple(prompts),), {"stop": stop, "sender": sender})
        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] batch")
                return cached
        future = self._qm.submit(method, prompts, stop=stop, sender=sender)
        out = future.result()
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    def generate(
        self,
        messages: List[Union[BaseMessage, dict]],
        stop: Optional[List[str]] = None,
        callbacks: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_manager: Any = None,
        sender: Optional[str] = None,
    ) -> ChatResult:
        method = "generate"
        msgs = self._normalize_messages(messages)
        msgs = [msgs]
        key = self._make_key(
            method,
            (tuple(m.dict() for m in msgs[0]),),
            {"stop": stop, "sender": sender}
        )
        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] generate")
                return cached
        future = self._qm.submit(
            method,
            msgs,
            stop=stop,
            callbacks=callbacks,
            tags=tags,
            metadata=metadata,
            run_manager=run_manager,
            sender=sender,
        )
        out = future.result()
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    def stream(
        self,
        messages: List[Union[BaseMessage, dict]],
        stop: Optional[List[str]] = None,
        callbacks: Optional[List[Any]] = None,
        sender: Optional[str] = None,
    ) -> Iterator[ChatResult]:
        method = "stream"
        msgs = self._normalize_messages(messages)
        key = self._make_key(
            method,
            (tuple(m.dict() for m in msgs),),
            {"stop": stop, "sender": sender}
        )
        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] stream")
                yield from cached
                return
        future = self._qm.submit(
            method,
            msgs,
            stop=stop,
            callbacks=callbacks,
            sender=sender,
        )
        gen = future.result()
        for chunk in gen:
            yield chunk

    def invoke(
        self,
        input: Union[PromptValue, str],
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
        method = "invoke"
        key_args: tuple[Any, ...]
        if messages is not None:
            msgs = self._normalize_messages(messages)
            key_args = (tuple(m.dict() for m in msgs),)
        else:
            key_args = (input,)
        key = self._make_key(method, key_args, {"stop": stop, "sender": sender})

        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logger.info("[CACHE HIT] invoke")
                return cached

        future = self._qm.submit(
            method,
            input,
            messages,
            stop=stop,
            callbacks=callbacks,
            tags=tags,
            metadata=metadata,
            run_manager=run_manager,
            sender=sender,
        )
        out = future.result()
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    # Асинхронные алиасы
    async def apredict(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> str:
        return await self._qm.submit_async("predict", prompt, stop=stop, sender=sender)

    async def acomplete(self, prompt: str, stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        return await self._qm.submit_async("complete", prompt, stop=stop, sender=sender)

    async def abatch(self, prompts: List[str], stop: Optional[str] = None, sender: Optional[str] = None) -> LLMResult:
        return await self._qm.submit_async("batch", prompts, stop=stop, sender=sender)

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
        return await self._qm.submit_async(
            "generate",
            messages,
            stop=stop,
            callbacks=callbacks,
            tags=tags,
            metadata=metadata,
            run_manager=run_manager,
            sender=sender
        )

    async def astream(
        self,
        messages: List[Union[BaseMessage, dict]],
        stop: Optional[List[str]] = None,
        callbacks: Optional[List[Any]] = None,
        sender: Optional[str] = None,
    ) -> AsyncIterator[ChatResult]:
        gen = await self._qm.submit_async("stream", messages, stop=stop, callbacks=callbacks, sender=sender)
        for chunk in gen:
            yield chunk

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
        return await self._qm.submit_async(
            "invoke",
            input,
            messages,
            stop=stop,
            callbacks=callbacks,
            tags=tags,
            metadata=metadata,
            run_manager=run_manager,
            sender=sender,
        )


def create_custom_llm(**kwargs) -> CachedQueuedChatOpenAI:
    return CachedQueuedChatOpenAI(**{
        # ChatOpenAI параметры
        "base_url":        "https://api.llm7.io/v1",
        "model_name":      "deepseek-r1",
        "temperature":     "0.0",
        "openai_api_base": "https://api.llm7.io/v1",
        "openai_api_key":  "unused",
        "verbose":         True,
        "max_tokens":      "50000",
        **kwargs
    })

_llm_instance = None

def get_llm() -> CachedQueuedChatOpenAI:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = create_custom_llm()
    return _llm_instance

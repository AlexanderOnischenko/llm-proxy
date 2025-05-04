import os, time, threading, queue, sqlite3, json, hashlib, pickle, asyncio, logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Optional, AsyncIterator,   Iterator, Any, Dict, Union
from langchain_core.callbacks.stdout import StdOutCallbackHandler
from langchain_community.chat_models import ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage, AIMessage, BaseMessage
from langchain.schema import LLMResult, ChatResult, PromptValue, ChatGeneration
import inspect

def flatten_list(nested_list):
    flat_list = []
    for item in nested_list:
        if isinstance(item, (list, tuple)):  # Если элемент — список/кортеж
            flat_list.extend(flatten_list(item))  # Рекурсивно разворачиваем
        else:
            flat_list.append(item)  # Если не список, добавляем как есть
    return flat_list

# «Вшиваем» в StdOutCallbackHandler корректный on_chat_model_start
def _no_op_on_chat_model_start(self, serialized: dict, messages: list[Any], **kwargs):
    # если хотите — можете тут логировать
    print(f"[LLM START] model={serialized.get('model_name')}  messages={len(messages)}")
    # возвращаем что возвращает базовый контракт (обычно None)
    return None

StdOutCallbackHandler.on_chat_model_start = _no_op_on_chat_model_start

# ================
# system-wide config
# ================
MAX_CONCURRENT = 1
MAX_QUEUE     = 100

CACHE_ENABLED = True
CACHE_PATH    = "llm_cache.sqlite"
CACHE_TTL     = 600000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class _QueueManager:
    def __init__(self, real_llm: ChatOpenAI):
        self._llm = real_llm
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._q   = queue.Queue(maxsize=MAX_QUEUE)
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
        for _ in range(MAX_CONCURRENT):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()

    def _worker(self):
        while True:
            method, args, kwargs, fut = self._q.get()
            try:
                # 1) Фильтруем kwargs по сигнатуре
                fn   = getattr(self._llm, method)
                sig  = inspect.signature(fn)
                safe = {k: v for k, v in kwargs.items() if k in sig.parameters}
                
                # 3) Для 'generate' (и invoke→generate) — всегда batch из List[List[BaseMessage]]
                call_fn   = fn
                call_args = list(args)
                if method == "generate":
                    call_args = [[flatten_list(args)]] # batch
                    call_fn   = self._llm.generate

                # 4) Выполняем под семафором
                with self._sem:
                    res = call_fn(*call_args, **safe)

                # 5) сразу отдаём результат (без touch’а generations)
                fut.set_result(res)

            except Exception as e:
                fut.set_exception(e)
            finally:
                self._q.task_done()

    def submit(self, method: str, *args, **kwargs):
        fut = Future()
        try:
            self._q.put((method, args, kwargs, fut), block=False)
        except queue.Full:
            raise RuntimeError("LLM queue overflow")
        logging.info(f"Enqueued {method}; queue={self._q.qsize()}; sender={kwargs.get('sender')}")
        return fut.result()
    
    async def submit_async(self, method: str, *args, **kwargs):
        fut = Future()
        try:
            self._q.put((method, args, kwargs, fut), block=False)
        except queue.Full:
            raise RuntimeError("LLM queue overflow")
        
        try:
            return await asyncio.wrap_future(fut)
        except asyncio.CancelledError:
            fut.cancel()  # Отменяем задачу, если клиент отключился
            raise


class CachedQueuedChatOpenAI(ChatOpenAI):
    def __init__(self, **kwargs):
        # 1) инициализируем Runnable Pydantic-совместимо
        super().__init__(**kwargs)
        # 2) наш «реальный» клиент, на котором вызываем методы внутри воркеров
        real = ChatOpenAI(**kwargs)
        self._qm = _QueueManager(real)

        # 3) кэш, если включён
        if CACHE_ENABLED:
            self._cache_lock = threading.Lock()
            self._conn = sqlite3.connect(CACHE_PATH, check_same_thread=False)
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cache(
                  key   TEXT PRIMARY KEY,
                  value BLOB,
                  ts    INTEGER
                )
            """)
            if CACHE_TTL > 0:
                cutoff = int(time.time()) - CACHE_TTL
                cur.execute("DELETE FROM cache WHERE ts < ?", (cutoff,))
            self._conn.commit()

    def _make_key(self, method: str, args: tuple, kwargs: dict) -> str:
        # учтём sender автоматически, потому что он в kwargs
        blob = json.dumps({"method":method, "args":args, "kwargs":kwargs},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _get_cached(self, key: str):
        with self._cache_lock:
            row = self._conn.execute(
                "SELECT value,ts FROM cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        val, ts = row
        if CACHE_TTL>0 and time.time()-ts>CACHE_TTL:
            with self._cache_lock:
                self._conn.execute("DELETE FROM cache WHERE key=?", (key,))
                self._conn.commit()
            logging.info(f"[CACHE EXPIRED] {key}")
            return None
        return pickle.loads(val)

    def _set_cached(self, key: str, value: Any):
        blob = pickle.dumps(value)
        with self._cache_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache(key,value,ts) VALUES(?,?,?)",
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
                c = m.get("content","")
                if t == "system":
                    norm.append(SystemMessage(content=c))
                elif t in ("user","human"):
                    norm.append(HumanMessage(content=c))
                elif t in ("assistant","ai"):
                    norm.append(AIMessage(content=c))
                else:
                    raise ValueError(f"Unknown message type: {t}")
            else:
                raise ValueError(f"Unsupported message element: {m!r}")
        return norm

    # ——— синхронные API ———

    def predict(
        self,
        prompt: str,
        stop: Optional[str]=None,
        sender: Optional[str]=None,
    ) -> str:
        method = "predict"
        key = self._make_key(method, (prompt,), {"stop":stop,"sender":sender})
        if CACHE_ENABLED:
            h = self._get_cached(key)
            if h is not None:
                logging.info("[CACHE HIT] predict")
                return h
        out = self._qm.submit(method, prompt, stop=stop, sender=sender)
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    def complete(
        self,
        prompt: str,
        stop: Optional[str]=None,
        sender: Optional[str]=None,
    ) -> LLMResult:
        method = "complete"
        key = self._make_key(method, (prompt,), {"stop":stop,"sender":sender})
        if CACHE_ENABLED:
            h = self._get_cached(key)
            if h is not None:
                logging.info("[CACHE HIT] complete")
                return h
        out = self._qm.submit(method, prompt, stop=stop, sender=sender)
        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    def batch(
        self,
        prompts: List[str],
        stop: Optional[str]=None,
        sender: Optional[str]=None,
    ) -> LLMResult:
        method = "batch"
        key = self._make_key(method, (tuple(prompts),), {"stop":stop,"sender":sender})
        if CACHE_ENABLED:
            h = self._get_cached(key)
            if h is not None:
                logging.info("[CACHE HIT] batch")
                return h
        out = self._qm.submit(method, prompts, stop=stop, sender=sender)
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
        # нормализуем
        msgs = self._normalize_messages(messages)
        msgs=[msgs]
        key = self._make_key(
            method,
            (tuple(m.dict() for m in msgs[0]),),
            {"stop":stop, "sender":sender}
        )
        if CACHE_ENABLED:
            h = self._get_cached(key)
            if h is not None:
                logging.info("[CACHE HIT] generate")
                return h
        out: ChatResult = self._qm.submit(
            method,
            msgs,
            stop=stop,
            callbacks=callbacks,
            tags=tags,
            metadata=metadata,
            run_manager=run_manager,
            sender=sender,
        )
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
            {"stop":stop, "sender":sender}
        )
        if CACHE_ENABLED:
            h = self._get_cached(key)
            if h is not None:
                logging.info("[CACHE HIT] stream")
                yield from h
                return
        for chunk in self._qm.submit(
            method,
            msgs,
            stop=stop,
            callbacks=callbacks,
            sender=sender,
        ):
            yield chunk

    def invoke(
        self,
        input: Union[PromptValue, str],    # <- теперь опционально
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
        # собираем ключ по тому, что на руках
        if messages is not None:
            msgs = self._normalize_messages(messages)
            key_args = (tuple(m.dict() for m in msgs),)
        else:
            key_args = (input,)
        key = self._make_key(method, key_args, {"stop": stop, "sender": sender})

        if CACHE_ENABLED:
            cached = self._get_cached(key)
            if cached is not None:
                logging.info("[CACHE HIT] invoke")
                return cached

        out: ChatResult = self._qm.submit(
            method,
            input,
            msgs,
            stop=stop,
            callbacks=callbacks,
            tags=tags,
            metadata=metadata,
            run_manager=run_manager,
            sender=sender,
        )

        if CACHE_ENABLED:
            self._set_cached(key, out)
        return out

    # ——— async aliases ———

    async def apredict(self, prompt: str, stop: Optional[str]=None, sender: Optional[str]=None) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.predict(prompt, stop=stop, sender=sender))

    async def acomplete(self, prompt: str, stop: Optional[str]=None, sender: Optional[str]=None) -> LLMResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.complete(prompt, stop=stop, sender=sender))

    async def abatch(self, prompts: List[str], stop: Optional[str]=None, sender: Optional[str]=None) -> LLMResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.batch(prompts, stop=stop, sender=sender))

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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.generate(
                messages, stop=stop, callbacks=callbacks,
                tags=tags, metadata=metadata,
                run_manager=run_manager,
                sender=sender
            )
        )

    async def astream(
        self,
        messages: List[Union[BaseMessage, dict]],
        stop: Optional[List[str]] = None,
        callbacks: Optional[List[Any]] = None,
        sender: Optional[str] = None,
    ) -> AsyncIterator[ChatResult]:
        """
        Асинхронный аналог stream(): 
        собираем все чанки в список и отдаём их по одному с async yield.
        """
        loop = asyncio.get_running_loop()
        # получаем синхронный список чанков
        results: List[ChatResult] = await loop.run_in_executor(
            None,
            lambda: list(self.stream(messages, stop=stop, callbacks=callbacks, sender=sender))
        )
        for chunk in results:
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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.invoke(
                input,
                messages=messages,
                stop=stop,
                callbacks=callbacks,
                tags=tags,
                metadata=metadata,
                run_manager=run_manager,
                sender=sender,
            )
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

def get_llm():
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = create_custom_llm()
    return _llm_instance


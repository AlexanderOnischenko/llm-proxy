# llm/services/wrapper.py

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Protocol, Union, cast, runtime_checkable

from langchain_core.callbacks.stdout import StdOutCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompt_values import PromptValue

from llm.services.cache import SQLiteCache, make_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Silence verbose on_chat_model_start spam in logs
# ---------------------------------------------------------------------------
def _no_op_on_chat_model_start(self, serialized: dict, messages: list[Any], **kwargs: Any) -> None:
    logger.info("[LLM START] model=%s messages=%s", serialized.get("model_name"), len(messages))


StdOutCallbackHandler.on_chat_model_start = _no_op_on_chat_model_start


# ---------------------------------------------------------------------------
# Public (stable) async contract we rely on.
# We intentionally do NOT call provider-specific helpers like `.apredict/.agenerate/.acomplete`.
# ---------------------------------------------------------------------------

# Runnable input accepted by most LangChain chat models.
LanguageModelInput = Union[str, PromptValue, list[BaseMessage]]

# Wrapper convenience: accept dict messages and normalize to BaseMessage.
MessageLike = Union[BaseMessage, dict[str, Any]]
WrapperInput = Union[str, PromptValue, list[MessageLike]]


@runtime_checkable
class AsyncChatRunnable(Protocol):
    async def ainvoke(self, input: LanguageModelInput, config: Any = None, **kwargs: Any) -> BaseMessage:  # noqa: A002
        ...


class CachedQueuedChatOpenAI:
    """Async wrapper that adds cache + concurrency limit + in-flight de-dup.

    Despite the historical name, this wrapper is **not** tied to ChatOpenAI.
    It works with any LangChain chat model that implements the public Runnable
    async method: `ainvoke`.

    Public surface is intentionally small:
      - `ainvoke(...) -> BaseMessage`
      - `apredict(...) -> str` (thin compatibility facade)
      - `no_cache()` context manager
    """

    def __init__(
        self,
        *,
        model: AsyncChatRunnable,
        cache_enabled: bool = True,
        cache_path: str = ":memory:",
        cache_ttl: int = 0,
        max_concurrent: int = 4,
        bp_cache: SQLiteCache | None = None,
        cache: SQLiteCache | None = None,
    ) -> None:
        if cache is not None and bp_cache is None:
            bp_cache = cache

        self._model: AsyncChatRunnable = model

        self.cache_enabled = bool(cache_enabled)
        self.cache_path = cache_path
        self.cache_ttl = int(cache_ttl or 0)

        self._sem = asyncio.Semaphore(max(int(max_concurrent or 1), 1))
        self._inflight: dict[str, asyncio.Future[Any]] = {}

        # Shared cache backend (preferred). If not provided, create a private one.
        self._cache = bp_cache or SQLiteCache(
            path=self.cache_path,
            ttl_seconds=self.cache_ttl,
            enabled=self.cache_enabled,
        )

    # ------------------------------------------------------------------
    # utilities
    # ------------------------------------------------------------------

    @contextmanager
    def no_cache(self):
        prev = self.cache_enabled
        self.cache_enabled = False
        try:
            yield
        finally:
            self.cache_enabled = prev

    def _llm_fingerprint(self) -> Dict[str, Any]:
        """Best-effort fingerprint of params that affect generation."""
        fields = [
            "model",
            "model_name",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "base_url",
            "openai_api_base",
        ]
        fp: Dict[str, Any] = {}
        for name in fields:
            if hasattr(self._model, name):
                val = getattr(self._model, name)
                if val is not None:
                    fp[name] = val

        # model_kwargs can affect results (logit_bias, response_format, etc.)
        if hasattr(self._model, "model_kwargs"):
            mk = getattr(self._model, "model_kwargs")
            if mk:
                fp["model_kwargs"] = mk
        return fp

    def _make_key(self, method: str, *, input_key: Any, kwargs_key: dict[str, Any]) -> str:
        # Keep the historical namespace to avoid needless cache busting when upgrading.
        return make_key(
            namespace="langchain.chatopenai",
            data={
                "method": method,
                "input": input_key,
                "kwargs": kwargs_key,
                "llm": self._llm_fingerprint(),
            },
        )

    def _cache_get(self, key: str) -> Any | None:
        return self._cache.get(key)

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache.set(key, value)

    def _normalize_messages(self, messages: Iterable[Any]) -> list[BaseMessage]:
        norm: list[BaseMessage] = []
        for m in messages:
            if isinstance(m, BaseMessage):
                norm.append(m)
                continue

            if isinstance(m, dict):
                # Support both LC-style {type, content} and OpenAI-style {role, content}
                t = m.get("type") or m.get("role")
                c = m.get("content", "")
                if t == "system":
                    norm.append(SystemMessage(content=c))
                elif t in ("user", "human"):
                    norm.append(HumanMessage(content=c))
                elif t in ("assistant", "ai"):
                    norm.append(AIMessage(content=c))
                else:
                    raise ValueError(f"Unknown message type/role: {t!r}")
                continue

            raise ValueError(f"Unsupported message element: {m!r}")
        return norm

    @staticmethod
    def _message_dump(message: BaseMessage) -> dict[str, Any]:
        if hasattr(message, "model_dump"):
            return message.model_dump()  # type: ignore[no-any-return]
        if hasattr(message, "dict"):
            return message.dict()  # pyright: ignore[reportDeprecated]
        return dict(getattr(message, "__dict__", {}))

    async def _run_queued(self, key: str, coro_factory):
        existing = self._inflight.get(key)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = fut
        try:
            async with self._sem:
                result = await coro_factory()
                fut.set_result(result)
                return result
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

    # ------------------------------------------------------------------
    # public async API
    # ------------------------------------------------------------------

    async def ainvoke(
        self,
        input: WrapperInput,  # noqa: A002
        config: Any = None,
        *,
        stop: list[str] | None = None,
        sender: str | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """Invoke the underlying model using only the public Runnable API.

        - If `input` is a list of dict messages, it is normalized into `list[BaseMessage]`.
        - Cache key includes: input + stop + kwargs + model fingerprint.
        - `config` is forwarded to the underlying model but NOT included in cache key.
        """

        # Normalize messages if needed.
        lc_input: LanguageModelInput
        input_key: Any
        if isinstance(input, list):
            # `input` can be list[BaseMessage] or list[dict].
            msgs = self._normalize_messages(input)
            lc_input = msgs
            input_key = tuple(self._message_dump(m) for m in msgs)
        else:
            lc_input = cast(LanguageModelInput, input)
            input_key = input

        # Include stop/sender/kwargs in key. `sender` is internal and does not affect output.
        kwargs_key = {"stop": stop, **kwargs}
        key = self._make_key("ainvoke", input_key=input_key, kwargs_key=kwargs_key)

        if self.cache_enabled:
            cached = self._cache_get(key)
            if cached is not None:
                logger.info("[CACHE HIT] ainvoke, key = %s", key)
                return cast(BaseMessage, cached)
        logger.info("[CACHE MISS] ainvoke, key = %s", key)

        async def _call() -> BaseMessage:
            if stop is None:
                return await self._model.ainvoke(lc_input, config=config, **kwargs)
            return await self._model.ainvoke(lc_input, config=config, stop=stop, **kwargs)

        out = await self._run_queued(key, _call)
        if self.cache_enabled:
            self._cache_set(key, out)
        return out

    async def apredict(
        self,
        prompt: str,
        stop: str | None = None,
        sender: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Compatibility facade returning text.

        Implemented via `ainvoke` to avoid relying on provider-specific helpers.
        """

        stop_list = [stop] if stop else None
        msg = await self.ainvoke(prompt, stop=stop_list, sender=sender, **kwargs)
        return cast(Any, getattr(msg, "content", None)) or str(msg)

    # ------------------------------------------------------------------
    # prevent accidental sync usage
    # ------------------------------------------------------------------

    def _no_sync_guard(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Sync методы отключены. Используйте async API.")

    predict = _no_sync_guard
    complete = _no_sync_guard
    batch = _no_sync_guard
    generate = _no_sync_guard
    invoke = _no_sync_guard
    stream = _no_sync_guard

    def __getattr__(self, name: str) -> Any:
        if name in {"predict", "complete", "batch", "generate", "invoke", "stream"}:
            return self._no_sync_guard

        # Explicitly block provider helper methods that would bypass caching
        # and concurrency control if called on the underlying model.
        if name in {"agenerate", "acomplete", "abatch"}:
            raise AttributeError(
                f"{name} is not exposed by this wrapper. Use `ainvoke`/`apredict` instead."
            )
        return getattr(self._model, name)

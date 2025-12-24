# llm/services/chat_provider.py
from __future__ import annotations

import inspect
import json
import logging

from typing import Any, Protocol

import httpx
from openai import AsyncOpenAI, OpenAIError

from llm.config.settings import Settings as llm_Settnigs
from llm.models.chat import ChatCompletionRequest
from llm.services.cache import SQLiteCache, make_key
logger = logging.getLogger(__name__)

class ChatProvider(Protocol):
    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        ...

    async def aclose(self) -> None:
        ...


def _as_jsonable_content(content: Any) -> str:
    """GigaChat expects message.content to be a string.

    Our public schema allows str | list[str|dict] | dict. For non-str, we serialize.
    """
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(content)


def _dump_obj(obj: Any) -> Any:
    """Best-effort conversion to dict for SDK responses."""
    
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()  # type: ignore[no-any-return]
    if hasattr(obj, "dict"):
        return obj.dict()  # type: ignore[no-any-return]
    if hasattr(obj, "json"):
        try:
            return json.loads(obj.json())
        except Exception:  # noqa: BLE001
            pass
    return obj


def _build_httpx_client(settings: llm_Settnigs) -> httpx.AsyncClient:
    """Build an httpx client with optional mTLS and custom CA bundle."""

    cert_file = getattr(settings, "llm_cert_file", None)
    key_file = getattr(settings, "llm_key_file", None)
    ca_bundle = getattr(settings, "llm_ca_bundle_file", None)
    verify_ssl = bool(getattr(settings, "llm_verify_ssl", True))

    if bool(cert_file) ^ bool(key_file):
        raise ValueError("Both LLM_CERT_FILE and LLM_KEY_FILE must be set together")

    cert: tuple[str, str] | None = None
    if cert_file and key_file:
        cert = (str(cert_file), str(key_file))

    # httpx.verify accepts bool or path to CA bundle
    verify: bool | str = bool(verify_ssl)
    if ca_bundle:
        verify = str(ca_bundle)

    timeout = float(getattr(settings, "llm_timeout", 300.0) or 300.0)

    return httpx.AsyncClient(timeout=timeout, verify=verify, cert=cert)


class OpenAIChatProvider:
    """OpenAI-compatible provider using the official OpenAI SDK.

    Supports optional mTLS (cert+key) and custom CA bundle by injecting a configured
    httpx client into AsyncOpenAI.
    """

    def __init__(self, settings: llm_Settnigs, *, cache: SQLiteCache | None = None):
        self._base_url = str(settings.llm_api_base)
        self._default_model = settings.llm_model_name
        self._default_temperature = settings.llm_temperature
        self._default_top_p = settings.llm_top_p

        # Transport options (optional): mTLS cert/key, custom CA bundle, verify flag.
        # Build only if needed to avoid opening extra connections in the common case.
        self._http_client: httpx.AsyncClient | None = None
        if (
            getattr(settings, "llm_cert_file", None)
            or getattr(settings, "llm_key_file", None)
            or getattr(settings, "llm_ca_bundle_file", None)
            or not bool(getattr(settings, "llm_verify_ssl", True))
        ):
            self._http_client = _build_httpx_client(settings)

        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=self._base_url,
            timeout=float(getattr(settings, "llm_timeout", 300.0) or 300.0),
            http_client=self._http_client,
        )

        # Prefer injected shared cache (from deps.build_cache).
        if cache is not None:
            self._cache = cache
        else:
            cache_path = getattr(settings, "llm_cache_path_resolved", None) or settings.llm_cache_path
            self._cache = SQLiteCache(
                path=str(cache_path),
                ttl_seconds=int(getattr(settings, "llm_cache_ttl", 0) or 0),
                enabled=bool(getattr(settings, "llm_cache_enabled", False)),
            )

    def _cache_key(self, payload: dict[str, Any]) -> str:
        digest = make_key(
            namespace="openai.chat.completions.create",
            data={"base_url": self._base_url, "payload": payload},
        )
        # Prefix is only for readability/debugging.
        return f"chat:{digest}"

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        payload = request.to_provider_payload(
            default_model=self._default_model,
            default_temperature=self._default_temperature,
            default_top_p=self._default_top_p,
        )

        key = self._cache_key(payload)

        async def _call_provider() -> dict[str, Any]:
            try:
                response = await self._client.chat.completions.create(**payload)
            except OpenAIError:
                logger.exception("Provider error while executing chat completion")
                raise

            if hasattr(response, "model_dump"):
                return response.model_dump()  # type: ignore[no-any-return]
            if hasattr(response, "dict"):
                return response.dict()  # type: ignore[no-any-return]
            return response  # type: ignore[no-any-return]

        def _on_hit(k: str) -> None:
            logger.info("[CACHE HIT] /v1/chat key=%s model=%s", k, payload.get("model"))

        def _on_miss(k: str) -> None:
            logger.info("[CACHE MISS] /v1/chat key=%s model=%s", k, payload.get("model"))

        return await self._cache.aget_or_set(key, _call_provider, on_hit=_on_hit, on_miss=_on_miss)

    async def aclose(self) -> None:
        # Best-effort close; provider lifetime is typically process-long.
        close = getattr(self._client, "close", None)
        if callable(close):
            res = close()
            if inspect.isawaitable(res):
                await res
        if self._http_client is not None:
            await self._http_client.aclose()


class GigaChatProvider:
    """Native GigaChat provider using the official `gigachat` SDK.

    GigaChat requires mTLS (client cert/key) for requests. CA bundle may also
    be required in some environments.
    """

    def __init__(self, settings: llm_Settnigs, *, cache: SQLiteCache | None = None):
        self._base_url = str(settings.llm_api_base)
        self._default_model = settings.llm_model_name

        # Local import: allow the rest of the project to work without gigachat extras.
        try:
            from langchain_gigachat import GigaChat  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "langchain_gigachat is required when LLM_PROVIDER=gigachat"
            ) from exc

        # NOTE: Settings names mirror gigachat SDK options.
        self._client = GigaChat(
            credentials=settings.llm_api_key,
            model=settings.llm_model_name,
            base_url=str(settings.llm_api_base),
            auth_url=getattr(settings, "llm_auth_url", None),
            scope=getattr(settings, "llm_scope", None),
            timeout=float(getattr(settings, "llm_timeout", 300.0) or 300.0),
            verify_ssl_certs=bool(getattr(settings, "llm_verify_ssl", True)),
            ca_bundle_file=getattr(settings, "llm_ca_bundle_file", None),
            cert_file=getattr(settings, "llm_cert_file", None),
            key_file=getattr(settings, "llm_key_file", None),
            key_file_password=getattr(settings, "llm_key_file_password", None),
        )

        if cache is not None:
            self._cache = cache
        else:
            cache_path = getattr(settings, "llm_cache_path_resolved", None) or settings.llm_cache_path
            self._cache = SQLiteCache(
                path=str(cache_path),
                ttl_seconds=int(getattr(settings, "llm_cache_ttl", 0) or 0),
                enabled=bool(getattr(settings, "llm_cache_enabled", False)),
            )

    def _cache_key(self, payload: dict[str, Any]) -> str:
        digest = make_key(
            namespace="gigachat.chat",
            data={"base_url": self._base_url, "payload": payload},
        )
        return f"chat:{digest}"

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        model = request.model or self._default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": m.role, "content": _as_jsonable_content(m.content)}
                for m in request.messages
            ],
        }

        if request.temperature is not None:
            payload["temperature"] = float(request.temperature)
        if request.top_p is not None:
            payload["top_p"] = float(request.top_p)
        if request.max_tokens is not None:
            payload["max_tokens"] = int(request.max_tokens)
        if request.stop is not None:
            payload["stop"] = request.stop

        key = self._cache_key(payload)

        async def _call_provider() -> dict[str, Any]:
            response = await self._client.achat(payload)  # type: ignore[attr-defined]
            return _dump_obj(response)  # type: ignore[no-any-return]

        def _on_hit(k: str) -> None:
            logger.info("[CACHE HIT] /v1/chat key=%s provider=gigachat model=%s", k, model)

        def _on_miss(k: str) -> None:
            logger.info("[CACHE MISS] /v1/chat key=%s provider=gigachat model=%s", k, model)

        return await self._cache.aget_or_set(key, _call_provider, on_hit=_on_hit, on_miss=_on_miss)

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            res = close()
            if inspect.isawaitable(res):
                await res


def make_chat_provider(settings: llm_Settnigs, *, cache: SQLiteCache | None = None) -> ChatProvider:
    """Single place where provider compatibility is decided."""

    provider = (getattr(settings, "llm_provider", "openai") or "openai").lower()
    if provider not in {"openai", "gigachat"}:
        # Soft fallback based on base_url.
        base = str(getattr(settings, "llm_api_base", "") or "").lower()
        provider = "gigachat" if "gigachat" in base or "giga.chat" in base else "openai"

    if provider == "gigachat":
        return GigaChatProvider(settings, cache=cache)
    return OpenAIChatProvider(settings, cache=cache)

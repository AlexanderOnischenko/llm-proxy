# llm/services/chat_provider.py
from __future__ import annotations

import inspect
import json
import logging
import ssl
from pathlib import Path
from typing import Any, Protocol

import httpx
from openai import AsyncOpenAI, OpenAIError

from llm.config.settings import Settings as llm_Settnigs
from llm.models.chat import ChatCompletionRequest
from llm.services.cache import SQLiteCache, make_key

logger = logging.getLogger(__name__)

_RESERVED_RESPONSE_KEYS = {"x_headers"}  # service metadata; must NOT leak into JSON for some SDKs

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


def _normalize_response_dict(data: dict[str, Any]) -> dict[str, Any]:
    """
    Provider-agnostic response normalization.

    Why: some SDKs (notably GigaChat python) build a response model as:
      Model(x_headers=from_http_headers(response), **response.json())
    If our proxy returns `x_headers` inside JSON body, the SDK passes it twice -> TypeError.
    """
    # Do not mutate caller's dict (could be cached/shared)
    out = dict(data)

    # Remove reserved metadata keys
    for k in _RESERVED_RESPONSE_KEYS:
        out.pop(k, None)

    # Defensive: if something somewhere produced "python field names" instead of JSON aliases
    if "object_" in out and "object" not in out:
        out["object"] = out.pop("object_")
    if "id_" in out and "id" not in out:
        out["id"] = out.pop("id_")

    return out


def _dump_obj(obj: Any) -> Any:
    """Best-effort conversion to JSON-serializable primitives/dicts for SDK responses."""
    if isinstance(obj, dict):
        return _normalize_response_dict(obj)

    if hasattr(obj, "model_dump"):
        # pydantic v2
        try:
            data = obj.model_dump(by_alias=True)  # type: ignore[no-any-return]
        except TypeError:
            data = obj.model_dump()  # type: ignore[no-any-return]
        if isinstance(data, dict):
            return _normalize_response_dict(data)
        return data

    if hasattr(obj, "dict"):
        # pydantic v1
        try:
            data = obj.dict(by_alias=True)  # type: ignore[no-any-return]
        except TypeError:
            data = obj.dict()  # type: ignore[no-any-return]
        if isinstance(data, dict):
            return _normalize_response_dict(data)
        return data

    if hasattr(obj, "json"):
        try:
            data = json.loads(obj.json())
            if isinstance(data, dict):
                return _normalize_response_dict(data)
            return data
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

def _build_mtls_ssl_context(settings) -> ssl.SSLContext:
    cert = getattr(settings, "llm_cert_file", None)
    key = getattr(settings, "llm_key_file", None)

    if not cert or not key:
        raise ValueError("GigaChat mTLS requires both LLM_CERT_FILE and LLM_KEY_FILE")

    cert_path = Path(cert)
    key_path = Path(key)

    if not cert_path.is_file():
        raise FileNotFoundError(f"LLM_CERT_FILE not found: {cert_path}")
    if not key_path.is_file():
        raise FileNotFoundError(f"LLM_KEY_FILE not found: {key_path}")

    cafile = getattr(settings, "llm_ca_bundle_file", None) or None
    ctx = ssl.create_default_context(cafile=cafile)

    password = getattr(settings, "llm_key_file_password", None)
    if password:
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path), password=password)
    else:
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    return ctx

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

        # GigaChat in our deployments requires mTLS on every request.
        cert_file = getattr(settings, "llm_cert_file", None)
        key_file = getattr(settings, "llm_key_file", None)
        if not cert_file or not key_file:
            raise ValueError("GigaChat requires both LLM_CERT_FILE and LLM_KEY_FILE (mTLS)")

        # Local import: allow the rest of the project to work without gigachat extras.
        try:
            from gigachat import GigaChat  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "gigachat is required when LLM_PROVIDER=gigachat"
            ) from exc

        ctx = _build_mtls_ssl_context(settings)

        self._client = GigaChat(
            model=settings.llm_model_name,
            base_url=str(settings.llm_api_base),
            timeout=float(getattr(settings, "llm_timeout", 300.0) or 300.0),
            verify_ssl_certs=True,
            ssl_context=ctx,
            # ВАЖНО: чтобы не попасть в verify=str/cert=... ветку внутри httpx — лучше не передавать:
            ca_bundle_file=None,
            cert_file=None,
            key_file=None,
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
            # Local import: allow the rest of the project to work without gigachat extras.
            try:
                from gigachat.models import Chat, Messages  # type: ignore
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("gigachat is required when LLM_PROVIDER=gigachat") from exc

            chat_payload: dict[str, Any] = {
                "model": model,
                "messages": [
                    Messages(role=m["role"], content=m["content"]) for m in payload["messages"]
                ],
            }
            for k in ("temperature", "top_p", "max_tokens", "stop"):
                if k in payload:
                    chat_payload[k] = payload[k]

            # Drop unsupported keys depending on SDK version (pydantic v1/v2).
            fields = getattr(Chat, "__fields__", None) or getattr(Chat, "model_fields", None) or {}
            allowed = set(fields) if isinstance(fields, dict) else set(fields)
            filtered = {k: v for k, v in chat_payload.items() if k in allowed} if allowed else chat_payload

            chat = Chat(**filtered)  # type: ignore[arg-type]
            response = await self._client.achat(chat)  # type: ignore[attr-defined]
            return _dump_obj(response)  # type: ignore[no-any-return]

        def _on_hit(k: str) -> None:
            logger.info("[CACHE HIT] /v1/chat key=%s provider=gigachat model=%s", k, model)

        def _on_miss(k: str) -> None:
            logger.info("[CACHE MISS] /v1/chat key=%s provider=gigachat model=%s", k, model)

        return await self._cache.aget_or_set(key, _call_provider, on_hit=_on_hit, on_miss=_on_miss)

    async def aclose(self) -> None:
        # Best-effort close; provider lifetime is typically process-long.
        aclose = getattr(self._client, "aclose", None)
        if callable(aclose):
            res = aclose()
            if inspect.isawaitable(res):
                await res
            return

        close = getattr(self._client, "close", None)
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

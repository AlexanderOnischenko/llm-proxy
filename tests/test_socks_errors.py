from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from openai import OpenAIError

from llm.metrics import socks as socks_metrics
from llm.models.chat import ChatCompletionRequest, ChatMessage
from llm.services.cache import SQLiteCache
from llm.services.chat_provider import OpenAIChatProvider
from llm.services.transport import HttpClientFactory, SocksProxyError


python_socks = pytest.importorskip("python_socks")


class SocksSettings:
    def __init__(self, *, cache_path: str):
        self.llm_api_key = "test"
        self.llm_api_base = "http://example"
        self.llm_timeout = 30
        self.llm_default_model_name = "gpt-test"
        self.llm_model_override = None
        self.llm_temperature = 0.1
        self.llm_top_p = 1.0
        self.llm_cache_ttl = 0
        self.llm_cache_enabled = False
        self.llm_cache_path_resolved = cache_path
        self.llm_socks_enabled = True
        self.llm_socks_type = "socks5"
        self.llm_socks_host = "127.0.0.1"
        self.llm_socks_port = 1080
        self.llm_socks_username = "user"
        self.llm_socks_password = "pass"
        self.llm_socks_connect_timeout = 3.0
        self.llm_socks_read_timeout = 30.0
        self.llm_socks_provider_overrides = {}


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=None,
        messages=[ChatMessage(role="user", content="hi")],
        temperature=None,
    )


def _socks_errors_total(provider: str, reason: str) -> float:
    return socks_metrics._SOCKS_ERRORS_TOTAL.labels(  # noqa: SLF001
        provider=provider,
        reason=reason,
    )._value.get()


def _make_auth_error() -> Exception:
    auth_error_cls = getattr(python_socks, "ProxyAuthenticationError", None) or getattr(
        python_socks, "ProxyAuthError", None
    )
    if auth_error_cls is None:
        pytest.skip("python_socks auth error class is unavailable")
    try:
        return auth_error_cls("invalid credentials")
    except TypeError:
        return auth_error_cls()


@pytest.mark.asyncio
async def test_read_timeout_does_not_mark_socks_failure(tmp_path, monkeypatch):
    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=False)
    settings = SocksSettings(cache_path=cache.path)
    transport_factory = HttpClientFactory(cast(Any, settings))
    transport_factory.note_socks_success()
    provider = OpenAIChatProvider(cast(Any, settings), cache=cache, transport_factory=transport_factory)

    async def create(**payload):
        raise httpx.ReadTimeout("upstream timeout")

    client = provider._get_client("socks")
    monkeypatch.setattr(client.chat.completions, "create", create, raising=False)

    before = _socks_errors_total("openai", "timeout")
    with pytest.raises(httpx.ReadTimeout):
        await provider.complete(_request())
    after = _socks_errors_total("openai", "timeout")

    assert after == before
    status = transport_factory.socks_runtime_status()
    assert status.status == "OK"
    assert status.error is None


@pytest.mark.asyncio
async def test_proxy_auth_error_marks_socks_failure(tmp_path, monkeypatch):
    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=False)
    settings = SocksSettings(cache_path=cache.path)
    transport_factory = HttpClientFactory(cast(Any, settings))
    transport_factory.note_socks_success()
    provider = OpenAIChatProvider(cast(Any, settings), cache=cache, transport_factory=transport_factory)

    async def create(**payload):
        raise _make_auth_error()

    client = provider._get_client("socks")
    monkeypatch.setattr(client.chat.completions, "create", create, raising=False)

    before = _socks_errors_total("openai", "auth")
    with pytest.raises(SocksProxyError) as exc_info:
        await provider.complete(_request())
    after = _socks_errors_total("openai", "auth")

    assert after == before + 1
    assert exc_info.value.code == "SOCKS_AUTH_ERROR"
    status = transport_factory.socks_runtime_status()
    assert status.status == "ERROR"
    assert status.error == "SOCKS_AUTH_ERROR"


@pytest.mark.asyncio
async def test_openai_error_not_wrapped_as_socks_failure(tmp_path, monkeypatch):
    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=False)
    settings = SocksSettings(cache_path=cache.path)
    transport_factory = HttpClientFactory(cast(Any, settings))
    transport_factory.note_socks_success()
    provider = OpenAIChatProvider(cast(Any, settings), cache=cache, transport_factory=transport_factory)

    async def create(**payload):
        raise OpenAIError("rate limited")

    client = provider._get_client("socks")
    monkeypatch.setattr(client.chat.completions, "create", create, raising=False)

    before = _socks_errors_total("openai", "connect")
    with pytest.raises(OpenAIError):
        await provider.complete(_request())
    after = _socks_errors_total("openai", "connect")

    assert after == before
    status = transport_factory.socks_runtime_status()
    assert status.status == "OK"
    assert status.error is None

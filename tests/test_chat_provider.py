from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from llm.models.chat import ChatCompletionRequest, ChatMessage
from llm.services.cache import SQLiteCache
from llm.services.chat_provider import OpenAIChatProvider, make_chat_provider


class DummySettings:
    llm_cert_file: str | None = None
    llm_key_file: str | None = None
    llm_ca_bundle_file: str | None = None
    llm_verify_ssl: bool = True
    
    def __init__(self, *, cache_path: str):
        self.llm_api_key = "test"
        self.llm_api_base = "http://example"
        self.llm_timeout = 30
        self.llm_model_name = "gpt-test"
        self.llm_temperature = 0.1
        self.llm_top_p = 1.0
        self.llm_cache_ttl = 0
        self.llm_cache_enabled = True
        self.llm_cache_path_resolved = cache_path

        # Provider selection (default)
        self.llm_provider = "openai"

        # TLS/mTLS options (default off)
        self.llm_verify_ssl = True
        self.llm_ca_bundle_file = None
        self.llm_cert_file = None
        self.llm_key_file = None


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


@pytest.mark.asyncio
async def test_chat_provider_caches_identical_requests(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=True)
    settings = DummySettings(cache_path=cache.path)

    provider = OpenAIChatProvider(cast(Any, settings), cache=cache)

    # Install async stub for openai call
    calls = {"n": 0}

    async def create(**payload):
        calls["n"] += 1
        return _Resp({"echo": payload})

    # provider._client.chat.completions.create
    monkeypatch.setattr(provider._client.chat.completions, "create", create, raising=False)

    req = ChatCompletionRequest(
        model=None,
        messages=[ChatMessage(role="user", content="hi")],
        temperature=None,
    )

    r1 = await provider.complete(req)
    r2 = await provider.complete(req)

    assert calls["n"] == 1
    assert r1 == r2

    # We should see one miss and one hit, each with a key.
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "[CACHE MISS] /v1/chat key=" in logs
    assert "[CACHE HIT] /v1/chat key=" in logs

    import re
    assert re.search(r"\[CACHE MISS\] /v1/chat key=(?:[a-z0-9_-]+:)?[0-9a-f]{64}\b", logs)
    assert re.search(r"\[CACHE HIT\] /v1/chat key=(?:[a-z0-9_-]+:)?[0-9a-f]{64}\b", logs)


@pytest.mark.asyncio
async def test_chat_provider_cache_key_changes_when_params_change(tmp_path, monkeypatch):
    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=True)
    settings = DummySettings(cache_path=cache.path)
    provider = OpenAIChatProvider(cast(Any, settings), cache=cache)

    calls = {"n": 0}

    async def create(**payload):
        calls["n"] += 1
        return _Resp({"ok": True, "payload": payload})

    monkeypatch.setattr(provider._client.chat.completions, "create", create, raising=False)

    base = {
        "model": None,
        "messages": [ChatMessage(role="user", content="hi")],
    }

    req_t1 = ChatCompletionRequest(**base, temperature=0.2)
    req_t2 = ChatCompletionRequest(**base, temperature=0.7)

    await provider.complete(req_t1)
    await provider.complete(req_t2)

    # Different temperature => different cache key => two provider calls
    assert calls["n"] == 2


def test_make_chat_provider_selects_gigachat_when_requested(tmp_path, monkeypatch):
    pytest.importorskip("gigachat")

    import gigachat

    from llm.services.chat_provider import GigaChatProvider

    class DummyGigaChat:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            return None

        async def achat(self, req: Any) -> dict[str, Any]:
            return {"ok": True, "req": req}

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(gigachat, "GigaChat", DummyGigaChat)

    settings = DummySettings(cache_path=str(tmp_path / "cache.sqlite"))
    settings.llm_provider = "gigachat"
    settings.llm_cert_file = "/tmp/cert.pem"
    settings.llm_key_file = "/tmp/key.pem"

    provider = make_chat_provider(cast(Any, settings), cache=None)
    assert isinstance(provider, GigaChatProvider)


@pytest.mark.parametrize(
    "verify_ssl, ca_bundle, expected_verify",
    [
        (True, "/tmp/ca.pem", "/tmp/ca.pem"),
        (False, None, False),
    ],
)
def test_openai_provider_mtls_wiring(monkeypatch, tmp_path, verify_ssl, ca_bundle, expected_verify):
    """Ensure we pass cert/key and verify settings into the injected httpx client."""

    captured: dict[str, Any] = {}

    class DummyAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any):
            captured.update(kwargs)

        async def aclose(self) -> None:
            return None

    import llm.services.chat_provider as chat_provider

    monkeypatch.setattr(chat_provider.httpx, "AsyncClient", DummyAsyncClient)

    settings = DummySettings(cache_path=str(tmp_path / "cache.sqlite"))
    settings.llm_cert_file = "/tmp/cert.pem"
    settings.llm_key_file = "/tmp/key.pem"
    settings.llm_ca_bundle_file = ca_bundle
    settings.llm_verify_ssl = verify_ssl

    provider = OpenAIChatProvider(cast(Any, settings), cache=None)
    assert provider._http_client is not None
    assert captured["cert"] == ("/tmp/cert.pem", "/tmp/key.pem")
    assert captured["verify"] == expected_verify

from __future__ import annotations

import logging

import pytest


@pytest.mark.asyncio
async def test_wrapper_apredict_uses_cache(tmp_path, monkeypatch, caplog):
    # These tests require the real langchain dependencies.
    pytest.importorskip("langchain_openai")
    pytest.importorskip("langchain_core")

    from langchain_openai import ChatOpenAI
    from langchain_core.messages import AIMessage
    from pydantic import SecretStr

    from llm.services.cache import SQLiteCache
    from llm.services.wrapper import CachedQueuedChatOpenAI

    caplog.set_level(logging.INFO)

    calls = {"n": 0}

    async def fake_ainvoke(self, input, config=None, **kwargs):
        calls["n"] += 1
        return AIMessage(content=f"OUT:{input}:{kwargs.get('stop')}")

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fake_ainvoke, raising=True)

    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=True)

    model = ChatOpenAI(
        api_key=SecretStr("test"),
        base_url="http://example",
        model="gpt-test",
        temperature=0.0,
        verbose=False,
        model_kwargs={"max_tokens": 16},
    )

    llm = CachedQueuedChatOpenAI(
        model=model,
        cache_enabled=True,
        cache_path=cache.path,
        cache_ttl=0,
        max_concurrent=5,
        bp_cache=cache,
    )

    out1 = await llm.apredict("hi")
    out2 = await llm.apredict("hi")

    assert out1 == out2
    assert calls["n"] == 1

    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "[CACHE MISS] ainvoke" in logs
    assert "[CACHE HIT] ainvoke" in logs
    assert "key =" in logs


@pytest.mark.asyncio
async def test_wrapper_cache_key_includes_llm_params(tmp_path, monkeypatch):
    pytest.importorskip("langchain_openai")
    pytest.importorskip("langchain_core")

    from langchain_openai import ChatOpenAI
    from langchain_core.messages import AIMessage
    from pydantic import SecretStr

    from llm.services.cache import SQLiteCache
    from llm.services.wrapper import CachedQueuedChatOpenAI

    calls = {"n": 0}

    async def fake_ainvoke(self, input, config=None, **kwargs):
        calls["n"] += 1
        return AIMessage(content=f"OUT:{input}:{kwargs.get('stop')}")

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fake_ainvoke, raising=True)

    cache = SQLiteCache(path=str(tmp_path / "cache.sqlite"), ttl_seconds=0, enabled=True)

    model1 = ChatOpenAI(
        api_key=SecretStr("test"),
        base_url="http://example",
        model="gpt-test",
        temperature=0.1,
        verbose=False,
        model_kwargs={"max_tokens": 16},
    )

    llm1 = CachedQueuedChatOpenAI(
        model=model1,
        cache_enabled=True,
        cache_path=cache.path,
        cache_ttl=0,
        max_concurrent=5,
        bp_cache=cache,
    )

    model2 = ChatOpenAI(
        api_key=SecretStr("test"),
        base_url="http://example",
        model="gpt-test",
        temperature=0.9,
        verbose=False,
        model_kwargs={"max_tokens": 16},
    )

    llm2 = CachedQueuedChatOpenAI(
        model=model2,
        cache_enabled=True,
        cache_path=cache.path,
        cache_ttl=0,
        max_concurrent=5,
        bp_cache=cache,
    )

    await llm1.apredict("hi")
    await llm2.apredict("hi")

    # Different temperature should produce different cache key => two underlying calls
    assert calls["n"] == 2
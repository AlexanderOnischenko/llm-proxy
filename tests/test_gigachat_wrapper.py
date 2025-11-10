from __future__ import annotations

import asyncio
from typing import Any, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from llm.services.gigachat_wrapper import CachedQueuedGigaChat


@pytest.fixture()
def gigachat_stub(monkeypatch):
    from langchain_gigachat import GigaChat

    def fake_init(self, **kwargs: Any) -> None:
        object.__setattr__(self, "_init_kwargs", kwargs)

    async def fake_aget_models(self) -> List[str]:
        return ["giga-lite"]

    call_counter = {"ainvoke": 0}

    async def fake_ainvoke(self, *args: Any, **kwargs: Any) -> ChatResult:
        call_counter["ainvoke"] += 1
        message = AIMessage(content="pong")
        generation = ChatGeneration(message=message)
        return ChatResult.model_construct(generations=[[generation]])

    monkeypatch.setattr(GigaChat, "__init__", fake_init, raising=False)
    monkeypatch.setattr(GigaChat, "aget_models", fake_aget_models, raising=False)
    monkeypatch.setattr(GigaChat, "ainvoke", fake_ainvoke, raising=False)

    return call_counter


def test_gigachat_wrapper_async_methods(tmp_path, gigachat_stub):
    async def _scenario():
        llm = CachedQueuedGigaChat(
            cache_enabled=True,
            cache_path=str(tmp_path / "cache.sqlite"),
            cache_ttl=60,
            max_concurrent=2,
            base_url="https://gigachat.example/api/v1",
            auth_url="https://auth.example/token",
            credentials="secret",
            scope="scope",
            model="giga-lite",
        )

        models = await llm.aget_models()
        assert models == ["giga-lite"]

        scripted_messages = [
            SystemMessage(content="You are a tester."),
            HumanMessage(content="ping"),
        ]

        result = await llm.ainvoke(messages=scripted_messages)
        assert result.generations[0][0].message.content == "pong"

        # cache hit
        second = await llm.ainvoke(messages=scripted_messages)
        assert second.generations[0][0].message.content == "pong"
        assert gigachat_stub["ainvoke"] == 1

        with pytest.raises(RuntimeError):
            llm.invoke("ping")

        if getattr(llm, "_conn", None):
            llm._conn.close()  # type: ignore[attr-defined]

    asyncio.run(_scenario())

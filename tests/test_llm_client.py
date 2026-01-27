from __future__ import annotations

from llm.config.settings import Settings


def test_create_custom_llm_respects_global_max_concurrent(monkeypatch):
    from llm.services import client as client_module

    captured: dict[str, int] = {}

    class DummyClient:
        def __init__(self, **kwargs):
            captured["max_concurrent"] = kwargs["max_concurrent"]

    monkeypatch.setattr(client_module, "CachedQueuedChatOpenAI", DummyClient)

    settings = Settings.model_construct(
        llm_max_concurrent=3,
        llm_api_base="http://example",
        llm_default_model_name="gpt-test",
        llm_temperature=0.0,
        llm_api_key="x",
        llm_verbose=False,
        llm_max_tokens=16,
        llm_cache_enabled=False,
        llm_cache_path=":memory:",
        llm_cache_ttl=0,
    )

    client_module.create_custom_llm(settings)

    assert captured["max_concurrent"] == 3

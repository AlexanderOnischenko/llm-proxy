import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.outputs import LLMResult

pytestmark = pytest.mark.anyio

from prometheus_client import CONTENT_TYPE_LATEST  # noqa: E402

from llm.deps import get_chat_queue, get_llm, reset_chat_dependencies  # noqa: E402
from llm.main import app  # noqa: E402
from llm.models.chat import ChatCompletionRequest  # noqa: E402
from llm.services.priority_queue import PriorityChatQueue  # noqa: E402


def _chat_response(content: str, model: str | None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class StubProvider:
    def __init__(
        self,
        reply: str = "ok",
        delay: float = 0.0,
        default_model: str = "gpt-default",
        default_temperature: float | None = 0.2,
        default_top_p: float | None = 1.0,
    ):
        self.reply = reply
        self.delay = delay
        self.default_model = default_model
        self.default_temperature = default_temperature
        self.default_top_p = default_top_p
        self.started: list[str] = []
        self.seen_payloads: list[dict[str, Any]] = []

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        self.started.append(str(request.messages[0].content))
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = request.to_provider_payload(
            default_model=self.default_model,
            default_temperature=self.default_temperature,
            default_top_p=self.default_top_p,
        )
        self.seen_payloads.append(payload)
        return _chat_response(self.reply, payload["model"])


class SlowProvider(StubProvider):
    def __init__(self):
        super().__init__(reply="slow")
        self.gate = asyncio.Event()

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:  # type: ignore[override]
        self.started.append(str(request.messages[0].content))
        await self.gate.wait()
        return _chat_response(self.reply, request.model)

    def release(self) -> None:
        self.gate.set()


class DummyLLM:
    async def agenerate(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("Dummy LLM should not be invoked in tests")
    
class BlockingLLM:
    """Эмулирует wrapper, который скрывает provider helpers через __getattr__.

    Если /llm снова начнет делать hasattr(llm, "agenerate") ДО enqueue,
    этот тест поймает регрессию (вернётся 400 вместо 200).
    """

    def __init__(self) -> None:
        self.missing_lookups: list[str] = []

    def __getattr__(self, name: str):
        self.missing_lookups.append(name)
        if name in {"agenerate", "acomplete", "abatch"}:
            raise AttributeError(name)
        raise AssertionError(f"/llm chat path must not touch llm.{name}")


class LegacyLLM:
    """Stub для non-chat ветки /llm (prompt/input)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def agenerate(self, prompt: str | None = None, **kwargs: Any):
        if prompt is not None:
            self.prompts.append(prompt)

        class _Result:
            def model_dump(self) -> dict[str, Any]:
                return {"generations": [[{"text": "ok"}]]}

        return _Result()


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    app.dependency_overrides.clear()
    reset_chat_dependencies()
    yield
    app.dependency_overrides.clear()
    reset_chat_dependencies()


@pytest.fixture
def make_queue():
    def _make(provider, max_queue: int = 10, limits: dict[str, int] | None = None, default_priority: str = "p2"):
        queue_limits = limits or {"p1": 1, "p2": 1, "p3": 1}
        return PriorityChatQueue(provider.complete, queue_limits, max_queue, default_priority=default_priority)

    return _make


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


async def test_missing_model_uses_provider_default_model(client, make_queue):
    provider = StubProvider(default_model="deepseek-default")
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    response = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "deepseek-default"
    assert provider.seen_payloads[-1]["temperature"] == provider.default_temperature
    assert provider.seen_payloads[-1]["top_p"] == provider.default_top_p
    assert payload["request_id"].startswith("r_")


async def test_stream_true_rejected(client, make_queue):
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_invalid_priority_header(client, make_queue):
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers={"X-Priority": "p4"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_llm_validation_errors_return_invalid_request(client, make_queue):
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)
    app.dependency_overrides[get_llm] = lambda: DummyLLM()

    response = await client.post(
        "/llm",
        json={"method": "generate", "kwargs": {"messages": []}},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"


async def test_llm_unknown_method_with_messages_rejected(client, make_queue):
    provider = StubProvider()
    queue = make_queue(provider)
    app.dependency_overrides[get_chat_queue] = lambda: queue
    app.dependency_overrides[get_llm] = lambda: DummyLLM()

    response = await client.post(
        "/llm",
        json={"method": "bogus", "kwargs": {"messages": [{"role": "user", "content": "hi"}]}},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["detail"] == "Unknown LLM method: abogus"


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "hi"}],   # OpenAI-style
        [{"type": "human", "content": "hi"}],  # legacy-style
    ],
)
async def test_llm_chat_returns_llmresult_and_preserves_chat_params(client, make_queue, messages):
    provider = StubProvider(reply="ok")
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)
    app.dependency_overrides[get_llm] = lambda: DummyLLM()

    resp = await client.post(
        "/llm",
        json={
            "method": "generate",
            "kwargs": {
                "model": "gpt-4o-mini",
                "temperature": 0.1,
                "messages": messages,
            },
        },
    )

    assert resp.status_code == 200
    body = resp.json()

    # Hard back-compat: must validate as LangChain LLMResult (generations required).
    LLMResult.model_validate(body)
    assert body["generations"][0][0]["text"] == "ok"

    # Ensure chat knobs weren't silently dropped.
    assert provider.seen_payloads[-1]["model"] == "gpt-4o-mini"
    assert provider.seen_payloads[-1]["temperature"] == 0.1

    # Metrics headers for /llm chat path should be present.
    assert resp.headers["x-llm-proxy-request-id"].startswith("r_")
    assert resp.headers["x-llm-proxy-priority"] == "p2"


@pytest.mark.parametrize(
    "method,messages,expected_text",
    [
        ("generate", [{"role": "user", "content": "hi"}], "hi"),
        ("invoke",   [{"role": "user", "content": "invoke"}], "invoke"),
        ("generate", [{"type": "human", "content": "type_hi"}], "type_hi"),
        ("invoke",   [{"type": "human", "content": "type_invoke"}], "type_invoke"),
    ],
)
async def test_llm_chat_backcompat_enqueues_even_if_llm_blocks_helpers(
    client,
    make_queue,
    method: str,
    messages: list[dict[str, Any]],
    expected_text: str,
):
    provider = StubProvider(reply="ok")
    queue = make_queue(provider)
    app.dependency_overrides[get_chat_queue] = lambda: queue

    # ключевой момент: llm "ломает" hasattr/agenerate, но /llm chat должен жить
    app.dependency_overrides[get_llm] = lambda: BlockingLLM()

    response = await client.post(
        "/llm",
        json={
            "method": method,
            "kwargs": {
                "model": "gpt-4o-mini",
                "messages": messages,
            },
        },
    )

    assert response.status_code == 200
    assert provider.started == [expected_text]
    assert provider.seen_payloads[-1]["model"] == "gpt-4o-mini"
    assert response.headers["x-llm-proxy-priority"] == "p2"
    assert response.headers["x-llm-proxy-request-id"].startswith("r_")

    body = response.json()
    # Новый контракт: /llm(chat) возвращает LangChain LLMResult
    LLMResult.model_validate(body)
    # ответ ассистента проброшен в generations
    assert body["generations"][0][0]["text"] == "ok"

    # если ты кладёшь model в llm_output — можно усилить (не обязательно)
    # assert body.get("llm_output", {}).get("model") == "gpt-4o-mini"


async def test_llm_legacy_prompt_calls_llm_agenerate(client, make_queue):
    # Queue isn't used in this branch, but we still provide it as dependency.
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    llm = LegacyLLM()
    app.dependency_overrides[get_llm] = lambda: llm

    response = await client.post(
        "/llm",
        json={"method": "generate", "kwargs": {"prompt": "hello"}},
    )

    assert response.status_code == 200
    assert llm.prompts == ["hello"]
    assert response.json() == {"generations": [[{"text": "ok"}]]}


async def test_completions_returns_metrics_headers(client, make_queue):
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.headers["x-llm-proxy-priority"] == "p2"
    assert int(response.headers["x-llm-proxy-queue-wait-ms"]) >= 0
    assert int(response.headers["x-llm-proxy-provider-latency-ms"]) >= 0


async def test_overrides_temperature_and_top_p(client, make_queue):
    provider = StubProvider(default_temperature=0.5, default_top_p=0.7)
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "custom"}],
            "temperature": 0.1,
            "top_p": 0.9,
            "priority": "lowest",
        },
    )

    assert response.status_code == 200
    payload = provider.seen_payloads[-1]
    assert payload["temperature"] == 0.1
    assert payload["top_p"] == 0.9
    assert response.json()["request_id"].startswith("r_")


async def test_invoke_returns_metrics_in_body(client, make_queue):
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    response = await client.post(
        "/v1/chat/invoke",
        headers={"X-Priority": "p1"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "invoke"}]},
    )

    assert response.status_code == 200
    metrics = response.json().get("metrics")
    assert metrics["priority"] == "p1"
    assert metrics["queue_wait_ms"] >= 0
    assert metrics["provider_latency_ms"] >= 0
    assert response.json()["request_id"].startswith("r_")


async def test_priority_order_respected(client, make_queue):
    provider = StubProvider(delay=0.05)
    limits = {"p1": 1, "p2": 1, "p3": 1}
    queue = make_queue(provider, limits=limits)
    app.dependency_overrides[get_chat_queue] = lambda: queue

    async def _post(path: str, priority: str, content: str):
        return await client.post(
            path,
            headers={"X-Priority": priority},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}]},
        )

    responses = await asyncio.gather(
        _post("/v1/chat/completions", "p2", "p2"),
        _post("/v1/chat/completions", "p1", "p1"),
        _post("/v1/chat/completions", "p3", "p3"),
    )

    assert all(resp.status_code == 200 for resp in responses)
    assert set(provider.started[:3]) == {"p1", "p2", "p3"}
    assert provider.started.index("p1") < provider.started.index("p3")


async def test_zero_capacity_priority_rejected(client, make_queue):
    provider = StubProvider()
    queue = make_queue(provider, limits={"p1": 0, "p2": 1}, default_priority="p2")
    app.dependency_overrides[get_chat_queue] = lambda: queue

    response = await client.post(
        "/v1/chat/completions",
        headers={"X-Priority": "p1"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_queue_overflow(client, make_queue):
    provider = SlowProvider()
    queue = make_queue(provider, max_queue=1, limits={"p1": 1, "p2": 1, "p3": 1})
    app.dependency_overrides[get_chat_queue] = lambda: queue

    first_request = asyncio.create_task(
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "first"}]},
        )
    )
    await asyncio.sleep(0.05)

    second_request = asyncio.create_task(
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "second"}]},
        )
    )

    await asyncio.sleep(0.01)

    third_response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "third"}]},
    )

    assert third_response.status_code == 429
    assert third_response.json()["error"]["code"] == "queue_overflow"

    provider.release()
    completed_first = await first_request
    completed_second = await second_request
    assert completed_first.status_code == 200
    assert completed_second.status_code == 200


async def test_zero_max_queue_allows_immediate_dispatch_and_rejects_when_busy(client, make_queue):
    provider = SlowProvider()
    queue = make_queue(provider, max_queue=0, limits={"p1": 1, "p2": 1, "p3": 1})
    app.dependency_overrides[get_chat_queue] = lambda: queue

    first_request = asyncio.create_task(
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "first"}]},
        )
    )

    await asyncio.sleep(0.01)

    second_response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "second"}]},
    )

    assert second_response.status_code == 429
    assert second_response.json()["error"]["code"] == "queue_overflow"

    provider.release()
    completed_first = await first_request
    assert completed_first.status_code == 200


async def test_metrics_endpoint_returns_200(client, make_queue):
    provider = StubProvider()
    app.dependency_overrides[get_chat_queue] = lambda: make_queue(provider)

    await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    metrics_resp = await client.get("/metrics")

    assert metrics_resp.status_code == 200
    assert metrics_resp.headers["content-type"].startswith(CONTENT_TYPE_LATEST)
    body = metrics_resp.text
    assert "llm_proxy_requests_total" in body
    assert "llm_proxy_queue_wait_seconds" in body
    assert "llm_proxy_queue_length" in body
    assert "llm_proxy_in_flight" in body


def test_create_app_rejects_multiprocess_env(monkeypatch):
    from llm import main as main_module

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom")

    with pytest.raises(RuntimeError):
        main_module.create_app()

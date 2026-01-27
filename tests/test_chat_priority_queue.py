import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.anyio

from prometheus_client import CONTENT_TYPE_LATEST  # noqa: E402

from llm.config.settings import reset_settings_overrides  # noqa: E402
from llm.deps import (  # noqa: E402
    get_chat_queue,
    get_llm,
    reset_chat_dependencies_async,
)
from llm.main import app  # noqa: E402
from llm.models.chat import ChatCompletionRequest, ChatMessage  # noqa: E402
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


@pytest.fixture(autouse=True)
async def _cleanup_overrides():
    app.dependency_overrides.clear()
    await reset_chat_dependencies_async()
    reset_settings_overrides()
    yield
    app.dependency_overrides.clear()
    await reset_chat_dependencies_async()
    reset_settings_overrides()


@pytest.fixture
def make_queue():
    def _make(
        provider,
        max_queue: int = 10,
        limits: dict[str, int] | None = None,
        default_priority: str = "p2",
        max_concurrent_total: int | None = None,
    ):
        queue_limits = limits or {"p1": 1, "p2": 1, "p3": 1}
        total_limit = max_concurrent_total if max_concurrent_total is not None else max(sum(queue_limits.values()), 1)
        return PriorityChatQueue(
            provider.complete,
            queue_limits,
            max_queue,
            default_priority=default_priority,
            max_concurrent_total=total_limit,
        )

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
    queue = make_queue(provider, limits=limits, max_concurrent_total=1)
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


async def test_total_inflight_never_exceeds_global_limit():
    max_total = 3
    limits = {"p1": 2, "p2": 3, "p3": 1}
    inflight_lock = asyncio.Lock()
    gate = asyncio.Event()
    started = asyncio.Event()
    inflight = 0
    peak = 0

    async def handler(request: ChatCompletionRequest) -> dict[str, Any]:
        nonlocal inflight, peak
        async with inflight_lock:
            inflight += 1
            peak = max(peak, inflight)
            if inflight >= max_total:
                started.set()
        await gate.wait()
        async with inflight_lock:
            inflight -= 1
        return _chat_response(str(request.messages[0].content), request.model)

    queue = PriorityChatQueue(
        handler=handler,
        limits=limits,
        max_queue=20,
        default_priority="p1",
        max_concurrent_total=max_total,
    )

    tasks = []
    for idx, priority in enumerate(["p1", "p2", "p3", "p2", "p1", "p3", "p2", "p1", "p2", "p3"]):
        payload = ChatCompletionRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role="user", content=f"{priority}-{idx}")],
            temperature=None,
        )
        tasks.append(asyncio.create_task(queue.enqueue(str(idx), priority, payload, endpoint="/v1/chat")))

    await asyncio.wait_for(started.wait(), timeout=1)
    assert peak <= max_total

    gate.set()
    await asyncio.gather(*tasks)
    assert peak <= max_total


async def test_per_priority_limit_respected_with_global_limit():
    limits = {"p1": 1, "p2": 2, "p3": 2}
    inflight_lock = asyncio.Lock()
    gate = asyncio.Event()
    started = asyncio.Event()
    per_priority = {"p1": 0}
    peak_p1 = 0

    async def handler(request: ChatCompletionRequest) -> dict[str, Any]:
        nonlocal peak_p1
        priority = str(request.messages[0].content)
        async with inflight_lock:
            per_priority["p1"] += 1
            peak_p1 = max(peak_p1, per_priority["p1"])
            started.set()
        await gate.wait()
        async with inflight_lock:
            per_priority["p1"] -= 1
        return _chat_response(priority, request.model)

    queue = PriorityChatQueue(
        handler=handler,
        limits=limits,
        max_queue=10,
        default_priority="p1",
        max_concurrent_total=100,
    )

    tasks = []
    for idx in range(3):
        payload = ChatCompletionRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role="user", content="p1")],
            temperature=None,
        )
        tasks.append(asyncio.create_task(queue.enqueue(str(idx), "p1", payload, endpoint="/v1/chat")))

    await asyncio.wait_for(started.wait(), timeout=1)
    assert peak_p1 <= 1

    gate.set()
    await asyncio.gather(*tasks)
    assert peak_p1 <= 1


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

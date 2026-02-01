"""Minimal Python client for the LLM proxy chat completions endpoint."""
from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProxyClientConfig:
    base_url: str = "http://localhost:8000"
    api_key: str | None = None
    timeout_s: float = 60.0
    default_headers: dict[str, str] = field(default_factory=dict)
    priority_in_body: bool = False
    verify_ssl: bool = True


class LlmProxyClient:
    def __init__(self, config: ProxyClientConfig) -> None:
        self._config = config

    def chat_completions(
        self,
        payload: dict[str, Any],
        *,
        priority: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_body = dict(payload)
        if priority and self._config.priority_in_body and "priority" not in request_body:
            request_body["priority"] = priority

        request_headers = self._build_headers(priority=priority, extra_headers=headers)
        data = json.dumps(request_body).encode("utf-8")
        request = Request(
            url=f"{self._config.base_url.rstrip('/')}/v1/chat/completions",
            data=data,
            headers=request_headers,
            method="POST",
        )

        context = self._build_ssl_context()
        try:
            with urlopen(request, timeout=self._config.timeout_s, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"Proxy request failed ({exc.code}): {detail}") from exc

    def _build_headers(
        self, *, priority: str | None, extra_headers: dict[str, str] | None
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **self._config.default_headers,
        }
        if self._config.api_key:
            headers.setdefault("Authorization", f"Bearer {self._config.api_key}")
        if priority:
            headers["X-Priority"] = priority
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if self._config.base_url.startswith("https"):
            if self._config.verify_ssl:
                return ssl.create_default_context()
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return None


if __name__ == "__main__":
    client = LlmProxyClient(
        ProxyClientConfig(
            base_url="http://localhost:8000",
            api_key=None,
            default_headers={"X-Custom-Header": "value"},
            priority_in_body=False,
        )
    )
    response = client.chat_completions(
        {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "Привет!"}],
        },
        priority="p1",
        headers={"X-Trace-Id": "example"},
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))

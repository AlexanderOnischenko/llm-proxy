from __future__ import annotations

from prometheus_client import Counter

_SOCKS_REQUESTS_TOTAL = Counter(
    "llm_socks_requests_total",
    "Total LLM requests by SOCKS/direct transport",
    labelnames=["provider", "transport"],
)
_SOCKS_ERRORS_TOTAL = Counter(
    "llm_socks_errors_total",
    "Total SOCKS-related transport errors",
    labelnames=["provider", "reason"],
)
_REQUESTS_TRANSPORT_TOTAL = Counter(
    "llm_requests_transport_total",
    "Total LLM requests by transport",
    labelnames=["provider", "transport"],
)


def record_transport(provider: str, transport: str) -> None:
    # Keep the SOCKS-prefixed counter for legacy dashboards while also recording
    # the unified transport metric used by newer views.
    _SOCKS_REQUESTS_TOTAL.labels(provider=provider, transport=transport).inc()
    _REQUESTS_TRANSPORT_TOTAL.labels(provider=provider, transport=transport).inc()


def record_socks_error(provider: str, reason: str) -> None:
    _SOCKS_ERRORS_TOTAL.labels(provider=provider, reason=reason).inc()

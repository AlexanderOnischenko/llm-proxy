from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import quote, urlparse

import httpx
from httpx_socks import AsyncProxyTransport, SyncProxyTransport
from python_socks import (
    ProxyAuthenticationError,
    ProxyConnectionError,
    ProxyError,
    ProxyTimeoutError,
)

from llm.config.settings import Settings
from llm.logging_config import get_logger
from llm.metrics.socks import record_socks_error, record_transport
from llm.models.status import DoctorCheck, DoctorReport, SocksConfigView, SocksRuntimeStatus


logger = get_logger(__name__)

TransportChoice = Literal["direct", "socks"]


@dataclass(frozen=True)
class SocksErrorDetails:
    code: str
    reason: str
    allow_fallback: bool


class SocksProxyError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def _unwrap_exception(exc: Exception) -> Exception:
    current = exc
    while True:
        candidate = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if candidate is None or candidate is current:
            return current
        if isinstance(candidate, Exception):
            current = candidate
            continue
        return current


def _classify_socks_error(exc: Exception) -> SocksErrorDetails | None:
    target = _unwrap_exception(exc)
    if isinstance(target, ProxyAuthenticationError):
        return SocksErrorDetails(code="SOCKS_AUTH_ERROR", reason="auth", allow_fallback=True)
    if isinstance(target, (ProxyConnectionError, ProxyError, httpx.ProxyError, httpx.ConnectError)):
        return SocksErrorDetails(code="SOCKS_CONNECT_ERROR", reason="connect", allow_fallback=True)
    if isinstance(target, (ProxyTimeoutError, httpx.ConnectTimeout)):
        return SocksErrorDetails(code="SOCKS_TIMEOUT", reason="timeout", allow_fallback=True)
    if isinstance(target, httpx.ReadTimeout):
        return SocksErrorDetails(code="SOCKS_TIMEOUT", reason="timeout", allow_fallback=False)
    return None


class HttpClientFactory:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._direct_async_client: httpx.AsyncClient | None = None
        self._socks_async_client: httpx.AsyncClient | None = None
        self._direct_client: httpx.Client | None = None
        self._socks_client: httpx.Client | None = None
        self._socks_error: str | None = None
        self._socks_last_checked: datetime | None = None

    def socks_config_view(self) -> SocksConfigView:
        return SocksConfigView(
            enabled=self._settings.llm_socks_enabled,
            type=self._settings.llm_socks_type,
            endpoint=self._settings.llm_socks_endpoint,
            strict=self._settings.llm_socks_strict,
            connect_timeout_s=self._settings.llm_socks_connect_timeout,
            read_timeout_s=self._settings.llm_socks_read_timeout,
            username_set=bool(self._settings.llm_socks_username),
        )

    def socks_runtime_status(self) -> SocksRuntimeStatus:
        if not self._settings.llm_socks_enabled and not self._forced_providers():
            return SocksRuntimeStatus(status="DISABLED")
        if self._socks_error:
            return SocksRuntimeStatus(
                status="ERROR",
                error=self._socks_error,
                last_checked_at=self._socks_last_checked,
            )
        if self._socks_last_checked:
            return SocksRuntimeStatus(status="OK", last_checked_at=self._socks_last_checked)
        return SocksRuntimeStatus(status="ERROR", error="SOCKS_UNCHECKED")

    def resolve_policy(self, provider_name: str) -> TransportChoice:
        provider_key = provider_name.lower()
        policy = self._settings.llm_socks_provider_overrides.get(provider_key, "inherit")
        if policy == "disable":
            return "direct"
        if policy == "force":
            return "socks"
        return "socks" if self._settings.llm_socks_enabled else "direct"

    def get_async_client(self, transport: TransportChoice) -> httpx.AsyncClient:
        if transport == "direct":
            if self._direct_async_client is None:
                timeout = httpx.Timeout(self._settings.llm_timeout)
                self._direct_async_client = httpx.AsyncClient(timeout=timeout)
            return self._direct_async_client

        if self._socks_async_client is None:
            proxy_url = self._proxy_url()
            timeout = httpx.Timeout(
                connect=self._settings.llm_socks_connect_timeout,
                read=self._settings.llm_socks_read_timeout,
                write=self._settings.llm_socks_read_timeout,
                pool=self._settings.llm_socks_connect_timeout,
            )
            transport_layer = AsyncProxyTransport.from_url(proxy_url)
            self._socks_async_client = httpx.AsyncClient(transport=transport_layer, timeout=timeout)
        return self._socks_async_client

    def get_client(self, transport: TransportChoice) -> httpx.Client:
        if transport == "direct":
            if self._direct_client is None:
                timeout = httpx.Timeout(self._settings.llm_timeout)
                self._direct_client = httpx.Client(timeout=timeout)
            return self._direct_client

        if self._socks_client is None:
            proxy_url = self._proxy_url()
            timeout = httpx.Timeout(
                connect=self._settings.llm_socks_connect_timeout,
                read=self._settings.llm_socks_read_timeout,
                write=self._settings.llm_socks_read_timeout,
                pool=self._settings.llm_socks_connect_timeout,
            )
            transport_layer = SyncProxyTransport.from_url(proxy_url)
            self._socks_client = httpx.Client(transport=transport_layer, timeout=timeout)
        return self._socks_client

    async def execute(
        self,
        provider_name: str,
        target_url: str,
        request_fn: Callable[[TransportChoice], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        transport = self.resolve_policy(provider_name)
        target_host = urlparse(target_url).netloc or target_url

        if transport == "direct":
            logger.debug("provider=%s transport=direct target=%s", provider_name, target_host)
            record_transport(provider_name, "direct")
            return await request_fn("direct")

        logger.debug("provider=%s transport=socks target=%s", provider_name, target_host)
        record_transport(provider_name, "socks")
        try:
            result = await request_fn("socks")
            self._mark_socks_ok()
            return result
        except Exception as exc:  # noqa: BLE001
            details = _classify_socks_error(exc)
            if details is None:
                raise
            self._mark_socks_error(details.code)
            record_socks_error(provider_name, details.reason)
            if not self._settings.llm_socks_strict and details.allow_fallback:
                logger.warning(
                    "SOCKS unavailable, falling back to direct (provider=%s reason=%s)",
                    provider_name,
                    details.code,
                )
                logger.debug(
                    "provider=%s transport=direct target=%s",
                    provider_name,
                    target_host,
                )
                record_transport(provider_name, "direct")
                return await request_fn("direct")
            logger.error(
                "SOCKS strict mode enabled, aborting request (provider=%s reason=%s)",
                provider_name,
                details.code,
            )
            raise SocksProxyError(details.code, str(exc)) from exc

    async def run_doctor(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        impact: list[str] = []
        provider_forces = self._forced_providers()
        socks_required = self._settings.llm_socks_enabled or bool(provider_forces)

        if not socks_required:
            checks.append(
                DoctorCheck(name="socks_disabled", ok=True, details="SOCKS is disabled")
            )
            impact.append("SOCKS disabled; all providers use direct transport")
            return DoctorReport(checks=checks, impact=impact)

        if self._settings.llm_socks_type != "socks5":
            checks.append(
                DoctorCheck(
                    name="config",
                    ok=False,
                    details=f"Unsupported SOCKS type: {self._settings.llm_socks_type}",
                )
            )
            impact.append("SOCKS proxy type unsupported; providers requiring SOCKS will fail")
            return DoctorReport(checks=checks, impact=impact)

        checks.append(DoctorCheck(name="config", ok=True, details="SOCKS config loaded"))

        tcp_ok, tcp_detail = await self._check_tcp_connect()
        checks.append(DoctorCheck(name="tcp_connect", ok=tcp_ok, details=tcp_detail))
        if not tcp_ok:
            impact.append("SOCKS proxy TCP connect failed; SOCKS transport unavailable")
            return DoctorReport(checks=checks, impact=impact)

        if self._settings.llm_socks_username:
            auth_ok, auth_detail = await self._check_auth()
            checks.append(DoctorCheck(name="auth", ok=auth_ok, details=auth_detail))
            if not auth_ok:
                impact.append("SOCKS authentication failed; SOCKS transport unavailable")
                return DoctorReport(checks=checks, impact=impact)

        outbound_ok, outbound_detail = await self._check_outbound()
        checks.append(
            DoctorCheck(name="outbound_test", ok=outbound_ok, details=outbound_detail)
        )
        if not outbound_ok:
            impact.append("Outbound test via SOCKS failed; upstream access may be blocked")

        if provider_forces:
            impact.append(
                "Providers requiring SOCKS: " + ", ".join(sorted(provider_forces))
            )
        return DoctorReport(checks=checks, impact=impact)

    async def aclose(self) -> None:
        clients = [
            self._direct_async_client,
            self._socks_async_client,
        ]
        for client in clients:
            if client is not None:
                await client.aclose()
        sync_clients = [
            self._direct_client,
            self._socks_client,
        ]
        for client in sync_clients:
            if client is not None:
                client.close()

    def close(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(self.aclose())
            return
        asyncio.run(self.aclose())

    def _proxy_url(self) -> str:
        auth = ""
        if self._settings.llm_socks_username:
            username = quote(self._settings.llm_socks_username, safe="")
            password = quote(self._settings.llm_socks_password or "", safe="")
            auth = f"{username}:{password}@"
        return f"{self._settings.llm_socks_type}://{auth}{self._settings.llm_socks_endpoint}"

    def _mark_socks_ok(self) -> None:
        self._socks_error = None
        self._socks_last_checked = datetime.now(timezone.utc)

    def _mark_socks_error(self, code: str) -> None:
        self._socks_error = code
        self._socks_last_checked = datetime.now(timezone.utc)

    async def _check_tcp_connect(self) -> tuple[bool, str | None]:
        try:
            coro = asyncio.open_connection(
                self._settings.llm_socks_host,
                self._settings.llm_socks_port,
            )
            reader, writer = await asyncio.wait_for(
                coro, timeout=self._settings.llm_socks_connect_timeout
            )
            writer.close()
            await writer.wait_closed()
            return True, "TCP connection established"
        except Exception as exc:  # noqa: BLE001
            return False, f"TCP connect failed: {exc.__class__.__name__}"

    async def _check_auth(self) -> tuple[bool, str | None]:
        proxy_url = self._proxy_url()
        transport_layer = AsyncProxyTransport.from_url(proxy_url)
        timeout = httpx.Timeout(
            connect=self._settings.llm_socks_connect_timeout,
            read=self._settings.llm_socks_read_timeout,
            write=self._settings.llm_socks_read_timeout,
            pool=self._settings.llm_socks_connect_timeout,
        )
        async with httpx.AsyncClient(transport=transport_layer, timeout=timeout) as client:
            try:
                response = await client.get(str(self._settings.llm_api_base), follow_redirects=True)
                return True, f"Auth ok (HTTP {response.status_code})"
            except Exception as exc:  # noqa: BLE001
                details = _classify_socks_error(exc)
                if details and details.code == "SOCKS_AUTH_ERROR":
                    return False, "SOCKS authentication failed"
                return False, f"SOCKS auth check failed: {exc.__class__.__name__}"

    async def _check_outbound(self) -> tuple[bool, str | None]:
        proxy_url = self._proxy_url()
        transport_layer = AsyncProxyTransport.from_url(proxy_url)
        timeout = httpx.Timeout(
            connect=self._settings.llm_socks_connect_timeout,
            read=self._settings.llm_socks_read_timeout,
            write=self._settings.llm_socks_read_timeout,
            pool=self._settings.llm_socks_connect_timeout,
        )
        target_url = str(self._settings.llm_api_base)
        async with httpx.AsyncClient(transport=transport_layer, timeout=timeout) as client:
            try:
                response = await client.get(target_url, follow_redirects=True)
                return True, f"Outbound ok (HTTP {response.status_code})"
            except Exception as exc:  # noqa: BLE001
                return False, f"Outbound request failed: {exc.__class__.__name__}"

    def _forced_providers(self) -> list[str]:
        return [
            provider
            for provider, policy in self._settings.llm_socks_provider_overrides.items()
            if policy == "force"
        ]

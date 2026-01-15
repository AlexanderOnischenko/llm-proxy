# app/main.py
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

import llm.deps as llm_deps
from llm.config.settings import get_settings
from llm.logging_config import configure_logging, get_logger
from llm.metrics.prometheus import router as metrics_router
from llm.middleware.request_logging import DebugRequestLoggingMiddleware, RequestIdMiddleware
from llm.routers.chat import router as chat_router
from llm.routers.llm import router as llm_router
from llm.routers.status import router as status_router


def _assert_single_process_runtime() -> None:
    logger = get_logger(__name__)

    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        msg = "PROMETHEUS_MULTIPROC_DIR is unsupported; run with a single worker"
        logger.error(msg)
        raise RuntimeError(msg)

    web_concurrency = os.environ.get("WEB_CONCURRENCY")
    if web_concurrency and web_concurrency != "1":
        msg = "WEB_CONCURRENCY must be unset or 1 for the single-worker runtime"
        logger.error(msg)
        raise RuntimeError(msg)

    server_software = os.environ.get("SERVER_SOFTWARE", "").lower()
    if "gunicorn" in server_software:
        msg = "Gunicorn multi-worker runtime is unsupported; use uvicorn with one worker"
        logger.error(msg)
        raise RuntimeError(msg)


def create_app() -> FastAPI:
    # 1) Configure logging FIRST
    configure_logging()
    logger = get_logger(__name__)

    _assert_single_process_runtime()

    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cache = None
        transport_factory = None
        try:
            cache = llm_deps.build_cache(settings)
            transport_factory = llm_deps.build_http_client_factory(settings)
            llm_deps.build_llm(settings)
            provider = llm_deps.build_chat_provider(settings)
            llm_deps.build_chat_queue(settings, provider)
            override = provider.model_override
            override_display = override or "disabled"
            logger.info(
                "Startup: model-override=%s (%s)",
                override_display,
                "sender model ignored" if override else "sender model respected",
            )
            socks_config = transport_factory.socks_config_view()
            if socks_config.enabled:
                logger.info(
                    "SOCKS proxy enabled: %s://%s",
                    socks_config.type,
                    socks_config.endpoint,
                )
            else:
                logger.info("SOCKS proxy disabled")
            logger.info("SOCKS strict mode: %s", socks_config.strict)
            logger.info("Startup: cache initialized, dependencies warmed")
            yield
        finally:
            if cache is not None:
                try:
                    cache.close()
                except Exception:
                    logger.exception("Failed to close cache")
            if transport_factory is not None:
                await transport_factory.aclose()

    app = FastAPI(
        title="LLM Proxy API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # 2) request_id always (ELK / tracing)
    app.add_middleware(RequestIdMiddleware)

    # 3) debug request logging optional
    if settings.debug:
        app.add_middleware(DebugRequestLoggingMiddleware, max_body_bytes=4096)

    app.include_router(llm_router, tags=["llm"])
    app.include_router(chat_router, tags=["chat"])
    app.include_router(status_router, tags=["status"])
    app.include_router(metrics_router, tags=["metrics"])

    return app


app = create_app()

import os

# app/main.py
from fastapi import FastAPI, Request, status
from shared_pkg.config.settings import get_settings
from llm.routers.llm import router as llm_router
from llm.routers.chat import router as chat_router
from llm.metrics.prometheus import router as metrics_router
from shared_pkg.utils.logging_config import get_logger, configure_logging


logger = get_logger(__name__)

def _assert_single_process_runtime() -> None:
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
    # Настройка логирования
    configure_logging()

    _assert_single_process_runtime()

    # Загрузка настроек приложения
    settings = get_settings()

    # TODO разобраться как включить no-cache запрос в LLM на старте
    # @asynccontextmanager
    # async def lifespan(app: FastAPI):
    #     settings : Settings = get_settings()
    #     if not settings.is_testing:
    #         llm = build_llm(settings)
    #         with llm.no_cache():
    #             logger.info("LLM OK: %r", llm.generate("привет, как дела сегодня?"))
    #     else:
    #         logger.info("Skipping LLM check in %s", settings.environment)
    #     yield
    #     logger.info("Shutdown")

    # Создаем FastAPI-приложение, передавая debug из настроек
    app = FastAPI(
        title="LLM Proxy API",
        version="1.0.0",
#        lifespan=lifespan,
#        debug=settings.debug
    )
    
    @app.get("/health", tags=["health"], status_code=status.HTTP_200_OK)
    async def health():
        return {"status": "ok"}
    
    # Подключаем маршруты для ADR Processor
    app.include_router(
        llm_router,
        tags=["llm"]
    )
    app.include_router(chat_router, tags=["chat"])
    app.include_router(metrics_router, tags=["metrics"])

    # Добавляем middleware логирования запросов только в debug режиме
    if settings.debug:
        @app.middleware("http")
        async def log_requests(request: Request, call_next):
            # Читаем тело запроса (await!)
            raw_body = await request.body()
            try:
                body_text = raw_body.decode('utf-8')
            except UnicodeDecodeError:
                body_text = '<non-textual payload>'
            logger.debug(f"Incoming request: {request.method} {request.url} Body: {body_text}")
            response = await call_next(request)
            return response
    return app

# Создаем экземпляр приложения
app = create_app()

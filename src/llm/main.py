# app/main.py
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from shared_pkg.config.settings import Settings, get_settings
from llm.deps import build_llm
from llm.routers.llm import router as llm_router
import logging
from shared_pkg.utils.logging_config import get_logger, configure_logging


logger = get_logger(__name__)

def create_app() -> FastAPI:
    # Настройка логирования
    configure_logging()

    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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

    # Подключаем маршруты для ADR Processor
    app.include_router(
        llm_router,
        tags=["llm"]
    )

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

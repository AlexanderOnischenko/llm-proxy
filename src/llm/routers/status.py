from __future__ import annotations

from fastapi import APIRouter, Depends

from llm.config.settings import Settings, get_settings
from llm.deps import get_http_client_factory
from llm.models.status import DoctorReport
from llm.services.transport import HttpClientFactory

router = APIRouter()


@router.get("/status")
async def status(
    factory: HttpClientFactory = Depends(get_http_client_factory),
    settings: Settings = Depends(get_settings),
) -> dict:
    return {
        "status": "ok",
        "env": settings.environment,
        "socks": {
            "config": factory.socks_config_view(),
            "runtime": factory.socks_runtime_status(),
        },
    }


@router.get("/doctor", response_model=DoctorReport)
async def doctor(
    factory: HttpClientFactory = Depends(get_http_client_factory),
) -> DoctorReport:
    return await factory.run_doctor()

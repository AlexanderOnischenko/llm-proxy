from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SocksConfigView(BaseModel):
    enabled: bool
    required_by_overrides: bool
    forced_providers: list[str]
    type: str
    endpoint: str
    connect_timeout_s: float = Field(..., ge=0)
    read_timeout_s: float = Field(..., ge=0)
    username_set: bool


class SocksRuntimeStatus(BaseModel):
    status: Literal["OK", "ERROR", "DISABLED"]
    error: str | None = None
    last_checked_at: datetime | None = None


class DoctorCheck(BaseModel):
    name: str
    ok: bool
    details: str | None = None


class DoctorReport(BaseModel):
    checks: list[DoctorCheck]
    impact: list[str]

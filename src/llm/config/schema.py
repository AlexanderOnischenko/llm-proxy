from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, HttpUrl


class CacheCfg(BaseModel):
    enabled: bool = True
    path: str = ":memory:"
    ttl: int = 0
    max_concurrent: int = 4


class DefaultsCfg(BaseModel):
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: int = 60
    verbose: bool = False
    cache: CacheCfg = CacheCfg()


class ProviderBase(BaseModel):
    type: str
    model: Optional[str] = None


class GigaChatCfg(ProviderBase):
    type: Literal["gigachat"]
    url: HttpUrl
    auth_url: HttpUrl
    scope: str
    credentials: str
    verify_ssl: bool = True
    ca_bundle: Optional[str] = None


class OpenAICompatCfg(ProviderBase):
    type: Literal["openai_compatible"]
    base_url: HttpUrl
    api_key: str


class GoogleGeminiCfg(ProviderBase):
    type: Literal["google_gemini"]
    api_key: str


ProviderCfg = GigaChatCfg | OpenAICompatCfg | GoogleGeminiCfg


class LLMRootCfg(BaseModel):
    active: str
    defaults: DefaultsCfg = DefaultsCfg()
    providers: Dict[str, ProviderCfg]


class Root(BaseModel):
    llm: LLMRootCfg

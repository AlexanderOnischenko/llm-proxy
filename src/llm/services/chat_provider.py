from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI, OpenAIError

from shared_pkg.config.settings import Settings
from shared_pkg.utils.logging_config import get_logger
from llm.models.chat import ChatCompletionRequest


logger = get_logger(__name__)


class OpenAIChatProvider:
    def __init__(self, settings: Settings):
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=str(settings.llm_api_base),
            timeout=settings.llm_timeout,
        )
        self._default_model = settings.llm_model_name
        self._default_temperature = settings.llm_temperature
        self._default_top_p = settings.llm_top_p

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        payload = request.to_provider_payload(
            default_model=self._default_model,
            default_temperature=self._default_temperature,
            default_top_p=self._default_top_p,
        )

        try:
            response = await self._client.chat.completions.create(**payload)
        except OpenAIError:
            logger.exception("Provider error while executing chat completion")
            raise

        if hasattr(response, "model_dump"):
            return response.model_dump()
        if hasattr(response, "dict"):
            return response.dict()  # type: ignore[no-any-return]
        return response  # type: ignore[no-any-return]

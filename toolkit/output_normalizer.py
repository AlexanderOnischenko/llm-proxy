from langchain.schema import LLMResult, ChatResult
from typing import Any
from dataclasses import dataclass
import json

@dataclass
class RawLLMOutput:
    text: str
    finish_reason: str | None = None
    raw: Any = None          # исходный ChatResult, если нужен

class OutputNormalizer:
    """Приводит str, ChatResult, LLMResult к RawLLMOutput."""
    
    @staticmethod
    def normalize(resp: str | ChatResult | LLMResult) -> RawLLMOutput:
        if isinstance(resp, dict):
            # Если в словаре есть поле text или output, возьмём его, иначе — просто сериализуем весь dict
            if resp.get("text"):
                text = OutputNormalizer.normalize(resp.get("text")).text 
            if resp.get("output"):
                text = OutputNormalizer.normalize(resp.get("output")).text
            else:
                text = json.dumps(resp, ensure_ascii=False)
            return RawLLMOutput(text=text, raw=resp)
        if isinstance(resp, str):
            return RawLLMOutput(text=resp)
        if isinstance(resp, ChatResult):
            return RawLLMOutput(
                text=resp.generations[0].text,
                finish_reason=resp.generations[0].generation_info.get("finish_reason"),
                raw=resp,
            )
        if isinstance(resp, LLMResult):
            return RawLLMOutput(
                text=resp.generations[0].text,
                finish_reason=resp.generations[0].generation_info.get("finish_reason"),
                raw=resp,
            )
        raise TypeError(f"Unsupported type: {type(resp)}")
from langchain.schema import LLMResult, ChatResult
from typing import Any, List
from dataclasses import dataclass
import json
import re
import textwrap
from utils.unicode_utils import maybe_unicode_unescape


@dataclass
class RawLLMOutput:
    text: str
    finish_reason: str | None = None
    raw: Any = None          # исходный ChatResult, если нужен

class OutputNormalizer:
    """Приводит str, ChatResult, LLMResult к RawLLMOutput."""  
    @staticmethod
    def normalize_recursive(resp: str | ChatResult | LLMResult) -> RawLLMOutput:
        # Поддержка списка: нормализуем каждый элемент и соединяем через ",\n"
        if isinstance(resp, list):
            texts: List[str] = []
            for item in resp:
                item_text = OutputNormalizer.normalize(item).text
                texts.append(item_text)
            joined = ",\n".join(texts)
            return RawLLMOutput(text=joined, raw=resp)
        if isinstance(resp, dict):
            # Если в словаре есть поле text или output, возьмём его, иначе — просто сериализуем весь dict
            if "text" in resp:
                text = OutputNormalizer.normalize_recursive(resp.get("text")).text 
            elif "output" in resp:
                text = OutputNormalizer.normalize_recursive(resp.get("output")).text
            else:
                text = json.dumps(resp, ensure_ascii=False)
            return RawLLMOutput(text=text, raw=resp)
        if isinstance(resp, str):
            t = resp
            # 1) Убираем префикс Final Answer:
            t = re.sub(r'(?mi)^\s*final answer:','', t).strip()
            # 2) Убираем любые кодовые fence-блоки ```…``` целиком (ЦЕЛИКОМ не надо!!!)
            # удаляем все строки, которые состоят только из ``` или ```yaml
            t = re.sub(r'^[ \t]*```(?:yaml)?[ \t]*\n?', '', t, flags=re.MULTILINE)
            # 3) и аналогично удаляем закрывающие fence
            t = re.sub(r'\n?[ \t]*```[ \t]*$', '', t, flags=re.MULTILINE)
            # 4) Убираем одиночные или двойные кавычки вокруг
            t = t.strip("'\"").strip()
            # 5) Если после всех этих шагов ничего не осталось – считаем пустым
            if not t:
                return RawLLMOutput(text="")
            else:
                return RawLLMOutput(text=t)
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
    
    @staticmethod
    def normalize(resp: str | ChatResult | LLMResult) -> RawLLMOutput:
        llm_output_raw = OutputNormalizer.normalize_recursive(resp)
        # 2) Декодируем экранированные последовательности сразу:
        try:
            text = maybe_unicode_unescape(llm_output_raw.text)
        except Exception:
            return llm_output_raw
        # 3) Продолжаем уже существующие шаги очистки:
        text = textwrap.dedent(text).strip()
        return RawLLMOutput(
            text=text, 
            finish_reason=llm_output_raw.finish_reason,
            raw = llm_output_raw.raw,
            )

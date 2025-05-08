import re
import json
import ast
import yaml
from utils.extract_json import extract_json, extract_bracketed_json
from utils.logger import logger
from abc import ABC, abstractmethod
from typing import TypeVar, Generic, List, Dict, Any, Protocol, Union
from tests.crash_logger import crash_logger

from llm.toolkit.output_normalizer import RawLLMOutput
from utils.extract_yaml import extract_yaml, extract_yaml_new
from pydantic import ValidationError
from domain.models.adr import ADRInfo
from processors.adr_processor.models.schemas import ConflictResult, RelevantADRList, RelevantADR
from processors.adr_processor.models.schemas import MatchedADR
from processors.adr_processor.models.schemas import QuestionAnswer, QuestionAnswers

T_co = TypeVar('T_co', covariant=True)

class BaseParser(Protocol[T_co]):
    @abstractmethod
    def parse(self, raw: RawLLMOutput) -> T_co:
        """Разбирает нормализованный вывод LLM в доменную модель T."""
        ...


class TextParser(BaseParser[str]):
    """Парсер, возвращающий "как есть" текст ответа."""
    def parse(self, raw: RawLLMOutput) -> str:
        return raw.text

class JsonParser(BaseParser[Dict[str, Any]]):
    """Парсер JSON-ответов LLM."""
    def parse(self, raw: RawLLMOutput) -> Dict[str, Any]:
        try:
            return json.loads(raw.text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
    
class YamlBlockParser(BaseParser[str], ABC):
    fence = "```"
    doc_sep_pattern = re.compile(r"(?m)^---\s*$")
    section_pattern = re.compile(r"(?m)^##\s+")

    def __init__(self, key: str):
        self.key = key
    
    def preprocess(self, raw: RawLLMOutput) -> str:
        if isinstance(raw, str):
            return raw
        else:
            return raw.text.strip()
    
    def postprocess(self, data: Any ) -> Any:
        return data
    
    @crash_logger("yaml_parser")
    def parse(self, raw: RawLLMOutput) -> str:
        text = self.preprocess(raw)
        if not text.strip():
            return ""
        
        # 1) Попытка JSON/Python-литерала
        jblock = extract_bracketed_json(text)
        if jblock:
            data = None
            try:
                data = json.loads(jblock)
            except Exception:
                try:
                    data = ast.literal_eval(jblock)
                except Exception:
                    pass
            if isinstance(data, dict) and isinstance(data.get(self.key), (list, dict)):
                return yaml.dump({self.key: data[self.key]},
                                 sort_keys=False, allow_unicode=True)

        # 2) Ищем YAML-фрагмент
        escaped = re.escape(self.key)
        m = re.search(rf"(?im)(?<!\S){escaped}\s*:", text)
        if not m:
            raise ValueError(f"Не найден ни JSON-массив '{self.key}', ни YAML-ключ '{self.key}:'")
        snippet = text[m.start():]

        # 3) обрезаем по первому `---`
        snippet = self.doc_sep_pattern.split(snippet)[0]
        # 4) обрезаем по первому заголовку `## …`
        snippet = self.section_pattern.split(snippet)[0]
        # 5) убираем любой оставшийся fence
        snippet = snippet.split(self.fence)[0]

        # 6) Чистим и форматируем через extract_yaml
        clean_doc = extract_yaml_new(snippet.strip())
        
        doc = self.postprocess(clean_doc)
        return yaml.dump(
            doc,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            indent=2,
        )

class ComponentsParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="components") 
    
    def preprocess(self, raw: RawLLMOutput) -> str:
        text = super().preprocess(raw)
        # если явный маркер «ничего не надо обновлять» — вернём пустую строку
        if "NO_COMPONENTS_TO_UPDATE" in text:
            return ""
        # в остальных случаях — просто обрежем пробелы
        return text.strip()
    
    def postprocess(self, data: Any) -> Any:
        # normalize casing
        if self.key not in data:
            for k in list(data):
                if k.lower() == self.key:
                    data[self.key] = data.pop(k)
                    break

        if not isinstance(data.get(self.key), list):
            raise ValueError(f"В разобранном YAML нет списка '{self.key}'")

        return data  # no dump here!


class IntegrationParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="integration_view")

class DataModelParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="entities")

class UseCaseParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="use_cases") 

class LayeredFlowsParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="logical_view_result") 



class DelimitedParser(BaseParser[str]):
    """
    Общий парсер, обрезающий ответ между заданными маркерами.
    """
    def __init__(self, start_pattern: str, end_patterns: List[str]):
        self.start_pattern = start_pattern
        self.end_patterns = end_patterns

    def parse(self, raw: RawLLMOutput) -> str:
        snippet = raw.text
        # начало
        m = re.search(self.start_pattern, snippet, flags=re.DOTALL)
        if not m:
            raise ValueError(f"Start token not found: {self.start_pattern}")
        snippet = snippet[m.start():]
        # конец
        for pat in self.end_patterns:
            m_end = re.search(pat, snippet)
            if m_end:
                snippet = snippet[: m_end.start()]
                break
        return snippet.strip()


# Специализированные парсеры


class AdrParser(BaseParser[List[Dict[str, str]]]):  # simple ADR entry model
    """Парсит выход LLM в список ADR-записей."""
    def parse(self, raw: RawLLMOutput) -> List[Dict[str, str]]:
        markdown = raw.text
        parts = re.split(r"(?m)^# ADR:", markdown)[1:]
        adrs: List[Dict[str, str]] = []
        for part in parts:
            lines = part.strip().splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            adrs.append({"title": title, "body": body})
        return adrs
    
class SystemContextParser(DelimitedParser):
    """Парсит секцию System Context из Markdown-ответа."""
    def __init__(self):
        # Старт — заголовок System Context, конец — либо маркер, либо следующий заголовок
        super().__init__(
            start_pattern=r"(?mi)^#\s*System Context",
            end_patterns=[
                r"\n---\n Powered by llm7\.io"
            ]
        )


class DfdParser(BaseParser[Dict[str, str]]):
    """Парсит два блока DFD: diagram_file.mmd и traceability.md из Markdown-ответа."""
    def parse(self, raw: RawLLMOutput) -> Dict[str, str]:
        text = raw.text
        # Проверка на наличие секции Final Answer
        if not re.search(r"Final Answer", text, flags=re.DOTALL):
            raise ValueError("Нет блока Final Answer")
        # Определяем блоки
        blocks = ["diagram_file.mmd", "traceability.md"]
        positions: Dict[str, int] = {}
        for block in blocks:
            pattern = rf"(?m)^###\s*{re.escape(block)}"
            match = re.search(pattern, text)
            if not match:
                raise ValueError(f"Missing block: ### {block}")
            positions[block] = match.start()
        # Сортируем блоки по позиции
        sorted_blocks = sorted(positions.items(), key=lambda x: x[1])
        first_block, first_pos = sorted_blocks[0]
        second_block, second_pos = sorted_blocks[1]
        snippet1 = text[first_pos:second_pos].strip()
        snippet2 = text[second_pos:].strip()
        return {
            first_block: snippet1,
            second_block: snippet2,
        }
    
class FSMarkdownParser(BaseParser[Dict[str, str]]):
    """Парсит Markdown с блоками stories.md, coverage_matrix.md и gap_analysis.yaml."""
    def parse(self, raw: RawLLMOutput) -> Dict[str, str]:
        text = raw.text
        blocks = ["stories.md", "coverage_matrix.md", "gap_analysis.yaml"]
        positions: Dict[str, int] = {}
        for block in blocks:
            pattern = rf"(?m)^###\s*{re.escape(block)}"
            match = re.search(pattern, text)
            if not match:
                raise ValueError(f"Missing block: ### {block}")
            positions[block] = match.start()
        sorted_blocks = sorted(positions.items(), key=lambda x: x[1])
        result: Dict[str, str] = {}
        for i, (block, start) in enumerate(sorted_blocks):
            end = sorted_blocks[i+1][1] if i+1 < len(sorted_blocks) else len(text)
            snippet = text[start:end].strip()
            if block.lower().endswith(".yaml"):
                snippet = extract_yaml(snippet)
            result[block] = snippet
        return result
    

class ADRConflictParser(BaseParser[List[ConflictResult]]):
    
    @crash_logger("adr_conflict_parser")
    def parse(self, raw: RawLLMOutput) -> List[ConflictResult]:
        text = raw.text or ""
        # Пустой или только пробельный ввод → нет конфликтов
        if not text.strip():
            return []

        # Парсим JSON/AST-блок из текста
        try:
            data = extract_json(text)
        except ValueError:
            # не нашли/не смогли распарсить JSON → считаем, что конфликтов нет
            return []

        # Нормализуем данные к списку словарей
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            raise ValueError("Ожидался список или объект JSON для ADR конфликтов")

        results: List[ConflictResult] = []
        for item in items:
            if not isinstance(item, dict):
                # пропускаем всё, что не словарь
                logger.error(f"Неверный формат ConflictResult: {str(item)}")
                continue
            try:
                results.append(ConflictResult(**item))
            except ValidationError as e:
                raise ValueError(f"Ошибка валидации элемента ADRConflict: {e}") from e

        return results


# Unused?
class ADRSearchParser(BaseParser[RelevantADRList]):
    def __init__(self, available_adrs: List[dict]):
        self.available_adrs = available_adrs

    def parse(self, raw: RawLLMOutput) -> RelevantADRList:
        meta = extract_json(raw.text)
        matched_raw = meta.get("matched_adrs", [])
        if not isinstance(matched_raw, list):
            matched_raw = []
        matched_objs = []
        for item in matched_raw:
            if isinstance(item, dict):
                try:
                    matched_objs.append(MatchedADR(**item))
                except ValidationError:
                    continue

        # собираем карту ADRInfo
        adr_map = {}
        for a in self.available_adrs:
            if isinstance(a, dict) and a.get("adr_id"):
                try:
                    info = ADRInfo(**a)
                    adr_map[info.adr_id] = info
                except ValidationError:
                    continue

        results = []
        for m in matched_objs:
            info = adr_map.get(m.adr_id, ADRInfo(adr_id=m.adr_id))
            results.append(
                RelevantADR(
                    adr_id=m.adr_id,
                    title=info.title or "",
                    full_description=info.full_description,
                    relevance_score=m.relevance,
                    match_reason=m.rationale,
                )
            )
        return RelevantADRList(matches=results)


class ADRMatchParser(BaseParser[QuestionAnswers]):
    def parse(self, raw: RawLLMOutput) -> QuestionAnswers:
        data = extract_json(raw.text)

        def is_qa(d: Any) -> bool:
            return (
                isinstance(d, dict)
                and all(k in d for k in ("question", "answer", "confidence_score", "supporting_adrs"))
            )

        if is_qa(data):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            raise ValueError("Expected list of QA objects or single object")

        answers = []
        for obj in items:
            if not is_qa(obj):
                raise ValueError(f"Invalid QA item: {obj}")
            answers.append(
                QuestionAnswer(
                    question=obj["question"],
                    answer=obj["answer"],
                    confidence_score=float(obj["confidence_score"]),
                    supporting_adrs=obj["supporting_adrs"],
                )
            )
        return QuestionAnswers(answers=answers)


class TechStackParser(DelimitedParser):
    """Парсит секцию Technology Stack из Markdown-ответа."""
    def __init__(self):
        super().__init__(
            # Ищем заголовок «# Technology Stack»
            start_pattern=r"(?mi)^#\s*Technology Stack",
            # Обрезаем либо по маркеру Powered by, либо по следующему заголовку
            end_patterns=[
                r"\n---\n Powered by llm7\.io",
                r"(?m)^#\s*(?!Technology Stack)"
            ]
        )

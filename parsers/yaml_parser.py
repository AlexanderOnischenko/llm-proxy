# llm/parsers/yaml_parser.py

import re
import ast
import json
from typing import Any
from abc import ABC

import yaml
from llm.toolkit.parsers import BaseParser, OutputParserException
from llm.toolkit.output_normalizer import RawLLMOutput
from llm.parsers.yaml_normaliizer import YamlNormalizer
from utils.crash_logger import crash_logger

from utils.extract_yaml import extract_yaml_new, dump_yaml
from utils.extract_json import extract_bracketed_json

    
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
        # normalize casing
        if self.key not in data:
            for k in list(data):
                if k.lower() == self.key:
                    data[self.key] = data.pop(k)
                    break
        
        if not data.get(self.key):
            return None

        if not isinstance(data.get(self.key), (list,dict)):
            raise ValueError(f"В разобранном YAML нет списка '{self.key}'")
        return data  # no dump here!
    

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
                return dump_yaml({self.key: data[self.key]})

        # 2) Ищем YAML-фрагмент
        escaped = re.escape(self.key)
        m = re.search(rf"(?im)(?<!\S){escaped}\s*:", text)
        if not m:
            return ""
        snippet = text[m.start():]

        # 3) обрезаем по первому `---`
        snippet = self.doc_sep_pattern.split(snippet)[0]
        # 4) обрезаем по первому заголовку `## …`
        snippet = self.section_pattern.split(snippet)[0]
        # 5) убираем любой оставшийся fence
        snippet = snippet.split(self.fence)[0]

        # 6) Чистим и форматируем через extract_yaml
        normalized = YamlNormalizer().normalize(snippet.strip())
        clean_doc = yaml.safe_load(normalized)
        
        doc = self.postprocess(clean_doc)

        return dump_yaml(doc)

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
    
    @crash_logger("components_parser")
    def parse(self, raw: RawLLMOutput) -> str:
        return super().parse(raw)


class IntegrationParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="integration_view")
    
    @crash_logger("integrations_parser")
    def parse(self, raw: RawLLMOutput) -> str:
        return super().parse(raw)

class DataModelParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="entities")
    
    @crash_logger("data_model_parser")
    def parse(self, raw: RawLLMOutput) -> str:
        return super().parse(raw)

class UseCaseParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="use_cases")
    
    @crash_logger("use_case_parser")
    def parse(self, raw: RawLLMOutput) -> str:
        return super().parse(raw) 

class LayeredFlowsParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="logical_view_result")
    
    @crash_logger("layered_flow_parser")
    def parse(self, raw: RawLLMOutput) -> str:
        return super().parse(raw) 
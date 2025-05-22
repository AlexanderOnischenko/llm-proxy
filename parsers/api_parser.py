import re
from typing import List, Dict, Any

import yaml

from llm.toolkit.output_normalizer import RawLLMOutput
from llm.parsers.yaml_parser import YamlBlockParser
from llm.parsers.openapi_parser import OpenApiParser

from utils.extract_yaml import extract_yaml_new

from tests.crash_logger import crash_logger

class MultiDocParser:
    """
    Парсер .yaml-файла с мультidocument, выбирает первые четыре части:
    1) metadata
    2) openapi
    3) pythonSpec
    4) notes
    Все дополнительные документы отбрасываются.
    Каждый фрагмент очищается от ```yaml```/``` и передаётся в extract_yaml.
    """
    def __init__(self):
        # Регэксп для разделителя документов `---` на своей строке
        self.doc_sep = re.compile(r'(?m)^---\s*$')
        # Регэксп для удаления fence-блоков ``` или ```yaml
        self.fence_re = re.compile(r'(?m)^```(?:yaml)?\s*$')

    def split_docs(self, text: str) -> List[str]:
        parts = self.doc_sep.split(text)
        # Убираем пустые фрагменты
        parts = [p for p in parts if p.strip()]
        return parts

    def clean_doc(self, doc: str) -> str:
        # Убираем fence-блоки и лишние пробелы
        return '\n'.join(
            line for line in doc.splitlines()
            if not self.fence_re.match(line)
        ).strip()
    
    # def _extract_open_api(self, text: str) -> Dict[str, Any]:
    #     """
    #     Выделяет разделы root-уровня спецификации OpenAPI (openapi, info, servers,
    #     paths, components, security, securitySchemes и т.д.),
    #     парсит каждый через extract_yaml_new и собирает обратно в YAML.
    #     """
    #     ROOT_KEYS = ['openapi', 'info', 'servers', 'paths', 'components',
    #                 'security', 'securitySchemes', 'tags', 'externalDocs']
    #     # формируем шаблон для поиска разделов
    #     section_pattern = re.compile(
    #         r'(?ms)^(?P<key>' + '|'.join(ROOT_KEYS) + r'):\s*.*?' +
    #         r'(?=^(?:' + '|'.join(ROOT_KEYS) + r'):\s|\Z)',
    #         re.MULTILINE
    #     )
    #     total: Dict[str, Any] = {}
    #     for match in section_pattern.finditer(text):
    #         raw_section = match.group(0)
    #         # парсим раздел в структуру
    #         doc = extract_yaml_new(raw_section)
    #         key = match.group('key')
    #         # извлекаем поддокумент и снова дампим в YAML
    #         total[key]=doc.get(key, doc)

    #     return total

    @crash_logger("api_parser")
    def parse(self, raw: RawLLMOutput) -> Dict[str, Any]:
        """
        Возвращает dict с ключами:
          - metadata   (dict)
          - openapi    (dict)
          - pythonSpec (raw str или parsed структура)
          - notes      (list)
        """
        o_parser = OpenApiParser()
        parts = self.split_docs(raw.text)
        keys = ['metadata', 'openapi', 'pythonSpec', 'notes']
        processed_keys = set()
        result: Dict[str, Any] = {}
        for part in parts:
            clean = self.clean_doc(part)
            for key in keys:
                if (key in part) and not (key in processed_keys):
                    processed_keys.add(key)
                    # порядок выполнения if-ов важен, поскольку openapi может встречаться в несокльких местах
                    if 'pythonSpec' in part:
                        # Извлекаем весь текст после метки "pythonSpec:" (включая отступы)
                        # Предполагаем формат "pythonSpec: |\n<код>"
                        match = re.search(r'^pythonSpec\s*:\s*\|\s*\n', clean, flags=re.MULTILINE)
                        if match:
                            code_text = clean[match.end():]
                            result[key] = code_text.rstrip()
                        else:
                            # Если нет литерала, возвращаем всё после двоеточия
                            parts_line = clean.split(':', 1)
                            result[key] = parts_line[1].strip() if len(parts_line) > 1 else '' 
                        break
                    elif 'openapi' in part:
                        parsed = o_parser.parse(clean)
                        for root_key, root_val in parsed.items():
                            result[root_key] = root_val
                        break
                    else:
                        parser = YamlBlockParser(key)
                        parsed = parser.parse(RawLLMOutput(text=clean))
                        parsed_dict = yaml.safe_load(parsed)
                        for root_key, root_val in parsed_dict.items():
                            result[root_key] = root_val
                        break
        return result


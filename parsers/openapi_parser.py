import re
import yaml
from typing import Optional, Dict, Any, List
from llm.parsers.yaml_normaliizer import YamlNormalizer, default_line_processor, quote_arrays, prequote_line
from llm.parsers.yaml_parser import YamlBlockParser
from utils.extract_yaml import extract_yaml_no_beautify

from tests.crash_logger import crash_logger

class OpenApiNormalizer(YamlNormalizer):
    def __init__(self):
        super().__init__(line_processors=[
            default_line_processor,
            quote_arrays,
            prequote_line,
        ])

    def normalize(self, raw_text: str) -> str:
        cleaned = super().normalize(raw_text)
        lines = cleaned.split("\n")
        lines = self._semantic_indentation_fix(lines)
        return "\n".join(lines)

    def _semantic_indentation_fix(self, lines: List[str]) -> List[str]:
        """
        Fix cases where 'properties:' under 'items: type: object' is over-indented.
        """
        out: List[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            out.append(line)
            # detect 'items:' line
            if re.match(r"^\s*items:\s*$", line):
                # peek ahead for type: object and then an over-indented properties:
                if i + 2 < len(lines):
                    type_line = lines[i + 1]
                    prop_line = lines[i + 2]
                    m_type = re.match(r"^(?P<indent>\s*)type:\s*object\s*$", type_line)
                    m_prop = re.match(r"^(?P<indent>\s*)(properties:)\s*$", prop_line)
                    if m_type and m_prop:
                        # make properties: indent two spaces deeper than items:
                        base_indent = m_type.group("indent")
                        new_indent = base_indent + "  "
                        out.append(new_indent + "properties:")
                        i += 3
                        continue
            i += 1
        return out


# === (Existing normalizer code omitted for brevity) ===
# Assume YamlNormalizer and OpenApiNormalizer classes are defined above
# along with preprocess_and_load_yaml(raw: str, normalizer: Optional[YamlNormalizer])

class OpenApiParser:
    """
    Парсер OpenAPI: выделяет разделы уровень root-ключей,
    нормализует с помощью OpenApiNormalizer и парсит через yaml.safe_load.
    """
    ROOT_KEYS = [
        'openapi', 'info', 'servers', 'paths', 'components',
        'security', 'securitySchemes', 'tags', 'externalDocs'
    ]
    _section_pattern = re.compile(
        r'(?ms)^(?P<key>' + '|'.join(ROOT_KEYS) + r'):\s*.*?' +
        r'(?=^(?:' + '|'.join(ROOT_KEYS) + r'):\s|\Z)',
        re.MULTILINE
    )

    def __init__(self, normalizer: Optional[YamlNormalizer] = None):
        # Используем OpenApiNormalizer по умолчанию
        self.normalizer = normalizer or OpenApiNormalizer()

    @crash_logger("openapi_parser")
    def parse(self, text: str) -> Dict[str, Any]:
        """
        Разделяет текст на секции по ROOT_KEYS, нормализует каждую секцию
        и возвращает словарь с ключами-заголовками и их содержимым.
        """
        text = extract_yaml_no_beautify(text)
        total: Dict[str, Any] = {}
        for match in self._section_pattern.finditer(text):
            raw_section = match.group(0)
            key = match.group('key')
            # Нормализация построчно
            cleaned_yaml = self.normalizer.normalize(raw_section)
            # Загрузка уже исправленного YAML
            try:
                doc: Any = yaml.safe_load(cleaned_yaml)
            except yaml.YAMLError as e:
                raise ValueError(f"Ошибка парсинга секции '{key}': {e}\n=== YAML ===\n{cleaned_yaml}")
            # Вытаскиваем внутреннюю структуру
            total[key] = doc.get(key, doc)
        return total



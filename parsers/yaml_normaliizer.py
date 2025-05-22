import re
import yaml
from typing import List, Optional, Callable


# === Module-level compiled regex patterns ===
_LINE_VALUE_RE = re.compile(
        r'^(?P<prefix>^\s*([a-zA-Z0-9_-]+(?::[a-zA-Z0-9_-]+)*):\s*(?=\S))'  # ключ и ':'
        r'(?P<val>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^#\n]+?)'  # значение
        r'(?P<comment>\s*#.*)?$'  # комментарий вне кавычек
    )
_LIST_VALUE_RE = re.compile(
    r'^(?P<prefix>\s*-\s*)'                                    # отступ + "- "
    r'(?P<val>'                                                 # захватываем значение, которое может быть:
      r'"(?:[^"\\]|\\.)*"|'                                    #   — двойная-кавычная строка
      r'\'(?:[^\'\\]|\\.)*\'|'                                 #   — одинарная-кавычная строка
      r'[^#\n]+'                                               #   — или любая последовательность не-# и не-"\n"
    r')'
    r'(?P<comment>\s*#.*)?$'                                    # комментарий (нежадно)
)
_NEEDS_QUOTE_RE = re.compile(r"[\s:\[\]\{\},'" + r"\\]|^(?:true|false|null|[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)$",
                              re.IGNORECASE)
_NUMBER_RE = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
_BOOL_RE = re.compile(r'^(?:true|false)$', re.IGNORECASE)
_EMPTY_LIST_RE = re.compile(r'^\[\]$')
_ARRAY_RE = re.compile(r'^\[\s*[^,\]]+(?:\s*,\s*[^,\]]+)*\s*\]$')
_LIST_PATTERN = re.compile(r'^- ')                       # '- ' элемент
_MAPPING_PATTERN = re.compile(r'^[^\\-\\*][^:]+:')    # mapping


# === Common utilities ===

def split_yaml_lines(text: str) -> List[str]:
    """
    Split on real '\n' but ignore newlines inside double quotes.
    """
    lines: List[str] = []
    current: List[str] = []
    in_quote = False
    escape = False
    for ch in text:
        if ch == '"' and not escape:
            in_quote = not in_quote
        if ch == '\\' and not escape:
            escape = True
            current.append(ch)
            continue
        if ch == '\n' and not in_quote:
            lines.append(''.join(current))
            current = []
            escape = False
            continue
        current.append(ch)
        escape = False
    lines.append(''.join(current))
    return lines

def is_mapping_line(line: str) -> bool:
    """
    Проверяет, является ли строка YAML-маппингом (key: value), а не сигнатурой функции.
    """
    in_single = in_double = in_parens = False
    for i, c in enumerate(line):
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == '(' and not in_single and not in_double:
            in_parens = True
        elif c == ')' and not in_single and not in_double:
            in_parens = False
        elif c == ':' and not in_single and not in_double and not in_parens:
            # Есть неэкранированный двоеточие вне кавычек и скобок
            # Убедимся, что перед ним есть ключ
            key = line[:i].strip()
            if key:  # Можно добавить доп.проверки: не начинается с `-`, `*`, `?`
                return True
    return False

def scalar_looks_like_special_yaml(val: str) -> bool:
    val = val.strip()
    if not val:
        return False
    if val[0] in {'*', '&', '!', '>', '|', '@'}:
        return True
    if re.match(r'^[\[\]\{\},:\?\-]+$', val):  # только спец. символы
        return True
    if re.match(r'^![\w\-]*$', val):  # yaml tag
        return True
    if re.match(r'^[>|][-+]?$', val):  # folded or literal
        return True
    return False

# === Quoting logic ===

def scalar_needs_quotes(s: str) -> bool:
    if re.fullmatch(r'^[\w\./-]+$', s) and s.lower() not in ('true', 'false', 'null'):
        return False
    return bool(_NEEDS_QUOTE_RE.search(s))

def scalar_quote_style(s: str) -> Optional[str]:
    """
    Return '"' or "'" if needed, else None.
    """
    if not scalar_needs_quotes(s):
        return None
    if "'" in s:
        return '"'
    return "'"

def quote_scalar(s: str) -> str:
    style = scalar_quote_style(s)
    if style == '"':
        esc = s.replace('"', '\\"')
    else:
        esc = s.replace("'", "''")
    return f"{style}{esc}{style}"

# === Processing pipeline on list of lines ===

def decode_escapes(lines: List[str]) -> List[str]:
    escapes = {"\\n": "\n", "\\t": "\t", '\\"': '"', "\\'": "'"}
    out: List[str] = []
    for line in lines:
        parts = re.split(r'("(?:[^"\\]|\\.)*")', line)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            for esc, ch in escapes.items():
                seg = seg.replace(esc, ch)
            parts[i] = seg
        out.append(''.join(parts))
    return out


def normalize_continuations(lines: List[str]) -> List[str]:
    """
    Сливает строки-«продолжения», которые
    начинаются с отступа и не содержат ':' к предыдущей строке.
    Например:
      goal: Безопасное объединение
        старой и новых версий
    станет:
      goal: Безопасное объединениес старой и новых версий
    """
    result: List[str] = []
    block_pipe_indent = None   
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        # 1) Обнаружили начало block scalar (pipe literal indicator)
        if block_pipe_indent is None and stripped.rstrip().endswith('|'):
            block_pipe_indent = indent
            result.append(line)
            continue
        # 2) Если внутри block scalar — сливаем сюда все переносы
        if block_pipe_indent is not None and indent > block_pipe_indent:
            # склеиваем с пред. строкой через пробел
            result[-1] += ' ' + stripped
            continue
        # 3) Вышли из блока — сбрасываем режим
        if block_pipe_indent is not None and indent <= block_pipe_indent:
            block_pipe_indent = None
        is_list_item = stripped.startswith('- ')
        is_mapping  = bool(re.match(r"^\s*[^:\s].*?:", line))
        if (indent > 0 
            and block_pipe_indent is None
            and not is_list_item 
            and not is_mapping):
            # сливаем с предыдущей строкой
            if result:
                result[-1] += ' ' + stripped
            else:
                result.append(line)
        else:
            result.append(line)
    return result


def sanitize_escapes(lines: List[str]) -> List[str]:
    return [re.sub(r'\\(?![nt"\\])', r'\\\\', ln) for ln in lines]


def remove_unmatched(lines: List[str]) -> List[str]:
    """
    Remove unmatched brackets/braces/parentheses from each line individually,
    preserving quoted newlines by operating line-by-line.
    """
    cleaned_lines: List[str] = []
    for line in lines:
        # Track counts per line
        paren = brace = bracket = 0
        out_chars: List[str] = []
        for ch in line:
            if ch == '(':
                paren += 1; out_chars.append(ch)
            elif ch == ')' and paren > 0:
                paren -= 1; out_chars.append(ch)
            elif ch == '[':
                bracket += 1; out_chars.append(ch)
            elif ch == ']' and bracket > 0:
                bracket -= 1; out_chars.append(ch)
            elif ch == '{':
                brace += 1; out_chars.append(ch)
            elif ch == '}' and brace > 0:
                brace -= 1; out_chars.append(ch)
            elif ch not in ')]}':
                out_chars.append(ch)
        cleaned = ''.join(out_chars)
        # Fix unmatched quotes per line
        for quote in ('"', "'"):
            if cleaned.count(quote) % 2:
                idx = cleaned.rfind(quote)
                cleaned = cleaned[:idx] + cleaned[idx+1:]
        cleaned_lines.append(cleaned)
    return cleaned_lines


def quote_arrays(line: str) -> str:
    def repl(m: re.Match) -> str:
        items = [i.strip() for i in m.group(1).split(',')]
        new = [it if (re.fullmatch(r'".*"|\'.*\'', it) or not scalar_needs_quotes(it)) else quote_scalar(it)
               for it in items]
        return '[' + ', '.join(new) + ']'
    return re.sub(r'\[([^\]]*)\]', repl, line)

def prequote_mapping(mapping: str) -> str:
    """
    Оборачивает ключ в mapping (key: value), если он требует кавычек.
    Например: **some text**: val  →  "**some text**": val
    """
    # Парсим ключ и значение
    m = re.match(r'^(?P<key>[^:]+):\s*(?P<val>.*)$', mapping)
    if not m:
        return mapping  # если не соответствует — возвращаем как есть

    key = m.group('key').strip()
    val = m.group('val').strip()

    # Если ключ уже в кавычках — оставляем
    if key and key[0] in ('"', "'") and key[-1] == key[0]:
        return mapping

    # Ключ требует кавычек?
    if scalar_needs_quotes(key) or scalar_looks_like_special_yaml(key):
        return f'{quote_scalar(key)}: {val}'

    return mapping

def prequote_line(line: str) -> str:
    # mapping key:value
    m = _LINE_VALUE_RE.match(line)
    if m:
        prefix, val, comment = m.group('prefix'), m.group('val').strip(), m.group('comment') or ''
        if (not val or _NUMBER_RE.match(val) or _BOOL_RE.match(val)
            or _EMPTY_LIST_RE.match(val) or _ARRAY_RE.match(val)
            or (val[0] in ('"', "'") and val[-1] == val[0])):
            return line
        if scalar_needs_quotes(val):
            return f"{prefix}{quote_scalar(val)}{comment}"
        return line
    # list element
    lm = _LIST_VALUE_RE.match(line)
    if lm:
        prefix, val, comment = lm.group('prefix'), lm.group('val').strip(), lm.group('comment') or ''
        # если это на самом деле вложенный mapping (foo: bar) — пропускаем
        if is_mapping_line(val):
            return f"{prefix}{prequote_mapping(val)}{comment}"
        if (not val or _NUMBER_RE.match(val) or _BOOL_RE.match(val)
            or _EMPTY_LIST_RE.match(val) or _ARRAY_RE.match(val)
            or (val[0] in ('"', "'") and val[-1] == val[0])):
            return line
        if scalar_needs_quotes(val) | scalar_looks_like_special_yaml(val):
            return f"{prefix}{quote_scalar(val)}{comment}"
        return line
    return line

# === Indentation repair works per line list ===

def repair_indentation(lines: List[str]) -> List[str]:
    out: List[str] = []
    key_stack: List[int] = []          # уровни отступов ключей "foo:"
    list_indent_stack: List[int] = []  # отступы текущих элементов списка
    for line in lines:
        stripped = line.lstrip()
        indent   = len(line) - len(stripped)
        # 0) Если у нас уже были ключи, и на нулевом уровне
        #    встретим строку, не ключ и не list-item — это трейлер, выходим.
        if key_stack and indent == key_stack[0] \
           and not stripped.startswith("- ") \
           and not _MAPPING_PATTERN.match(stripped):
            if not stripped:
                continue
            elif not stripped.endswith("\n"):
                out.append("")
            break

        # «поднимаемся» — сбрасываем стеки
        while list_indent_stack and indent < list_indent_stack[-1]:
            list_indent_stack.pop()
        while key_stack and indent < key_stack[-1]:
            key_stack.pop()

        # элемент списка
        if _LIST_PATTERN.match(stripped):
            if key_stack:                      # список внутри карты
                target = key_stack[-1] + 2
                line = " " * target + stripped
                indent = target
            # запоминаем, какой отступ имеет этот элемент
            if not list_indent_stack or indent != list_indent_stack[-1]:
                list_indent_stack.append(indent)
            out.append(line)
            continue

        # строка-ключ карты
        if _MAPPING_PATTERN.match(stripped):
            # если ключ оказался на том же уровне, что и дефис списка → сдвигаем
            if list_indent_stack and indent == list_indent_stack[-1]:
                indent = list_indent_stack[-1] + 2
                line = " " * indent + stripped
            key_stack.append(indent)
            out.append(line)
            continue

        # остальные строки без изменений
        out.append(line)
    return out

# === Default line processors for common fixes ===
def default_line_processor(line: str) -> str:
    """
    Fix trailing colon on type values: 'type: object:' -> 'type: object'
    """
    return re.sub(r'^(?P<i>\s*type:\s*)(?P<v>[\w-]+):(\s*)$', r"\g<i>\g<v>", line)


# === Normalizer classes ===
class YamlNormalizer:
    def __init__(self, line_processors: Optional[List[Callable[[str], str]]] = None):
        self.line_processors = line_processors or []

    def normalize(self, raw_text: str) -> str:
        # 1) Split into lines respecting quoted newlines
        lines = split_yaml_lines(raw_text)
        # 2) Structural cleanup
        for fn in [decode_escapes, normalize_continuations, sanitize_escapes, remove_unmatched]:
            lines = fn(lines)
        # 3) Per-line fixes
        procs = self.line_processors or [default_line_processor, quote_arrays, prequote_line]
        for proc in procs:
            lines = [proc(ln) for ln in lines]
        # 4) Repair indentation last
        lines = repair_indentation(lines)
        return "\n".join(lines)
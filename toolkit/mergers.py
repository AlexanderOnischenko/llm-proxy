# llm/toolkit/mergers/base.py
import yaml
import copy
from pydantic import ValidationError, BaseModel
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union, Set, Type, ClassVar, get_origin, get_args
from utils.deep_merge_dicts import deep_merge_dicts
from sentence_transformers import SentenceTransformer, util
from functools import lru_cache
from tests.crash_logger import crash_logger
from domain.models.components import Component, ComponentsStructure, Structural, Semantic, PublicInterface
from utils.logger import logger
from utils.deep_get import deep_get
import torch
import inspect


# Сохраняем оригинальный метод
_orig_tensor_argmax = torch.Tensor.argmax
_orig_fn_argmax    = torch.argmax

def _safe_tensor_argmax(self, *args, **kwargs):
    # Если тензор лежит на MPS, сперва переносим на CPU
    if self.device.type == "mps":
        return self.cpu().argmax(*args, **kwargs)
    return _orig_tensor_argmax(self, *args, **kwargs)

def _safe_fn_argmax(input, *args, **kwargs):
    if isinstance(input, torch.Tensor) and input.device.type == "mps":
        return torch.argmax(input.cpu(), *args, **kwargs)
    return _orig_fn_argmax(input, *args, **kwargs)

# Патчим методы глобально
torch.Tensor.argmax = _safe_tensor_argmax
torch.argmax        = _safe_fn_argmax


class BaseMerger(ABC):
    @abstractmethod
    def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param old: разбранный из YAML/JSON словарь “старого” состояния
        :param new: разбранный из YAML/JSON словарь “нового” состояния
        :return: словарь с объединённым состоянием
        """
        ...


class ComponentMerger(BaseMerger):
    """
    Алгоритм “старое + новое → merged” взят из OldComponentsParser.merge
    Но тут мы работаем уже с Python-структурами, а не со строками.
    """
    @crash_logger("components_merge")
    def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        old_comps: List[Dict[str, Any]] = old.get("components", [])
        new_comps: List[Dict[str, Any]] = new.get("components", [])

        # вспомогательные мапы
        by_name = {c["name"]: c for c in old_comps if "name" in c}
        used_new_ids = set()
        merged: List[Dict[str, Any]] = []

        for old_c in old_comps:
            name = old_c.get("name")
            match = None

            # 1) точное совпадение
            if name in by_name and any(n.get("name") == name for n in new_comps):
                match = next(n for n in new_comps if n.get("name") == name)
                used_new_ids.add(id(match))

            # 2) семантическое совпадение (ваша логика)
            if not match:
                resp = old_c.get("semantic", {}).get("responsibility")
                if resp:
                    # ... ваш код с Levenshtein, threshold, best candidate ...
                    from Levenshtein import ratio
                    candidates = [
                        n for n in new_comps
                        if n.get("semantic", {}).get("responsibility") == resp
                    ]
                    best, best_ratio = None, 0.0
                    for c in candidates:
                        r = ratio(name or "", c.get("name",""))
                        if r > best_ratio:
                            best, best_ratio = c, r
                    if best_ratio >= 0.8:
                        match = best
                        used_new_ids.add(id(best))

            # 3) объединяем
            if match:
                merged.append(deep_merge_dicts(old_c, match))
            else:
                merged.append(old_c)

        # 4) добавляем все новые, что не попали
        for nc in new_comps:
            if id(nc) not in used_new_ids:
                merged.append(nc)

        # 5) финальный документ
        out = old.copy()
        # перезаписываем прочие поля из new, кроме components
        for k, v in new.items():
            if k != "components":
                out[k] = v
        out["components"] = merged
        return out


#1) Функция для ленивой загрузки модели — кешируется, так что будет вызвана ровно один раз
@lru_cache(maxsize=1)
def _load_st_model(model_name: str):
    from sentence_transformers import SentenceTransformer, util
    # Отключаем информационные логи
    import logging
    logging.getLogger("sentence_transformers.SentenceTransformer").setLevel(logging.WARNING)
    return SentenceTransformer(model_name), util


class SemanticYamlMerger(BaseMerger):
    """
    Семантический merge для корневого ключа (обычно "components"):
      1) точное совпадение по name,
      2) потом семантика по key_path,
      3) без дублирования новых элементов.
    """
    def __init__(
        self,
        root: str = "components",
        key_path: str = "semantic.responsibility",
        model_name: str = "all-MiniLM-L6-v2",
        emb_threshold_comp: float = 0.91,
        emb_threshold_key: float = 0.6
    ):
        self.root = root
        self.key_path = key_path
        self.emb_threshold_comp = emb_threshold_comp
        self.emb_threshold_key = emb_threshold_key
        self._model, self._util = _load_st_model(model_name)
    
    def post_process(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return result

    def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        old_list: List[Dict[str, Any]] = old.get(self.root, [])
        new_list: List[Dict[str, Any]] = new.get(self.root, [])

        # Если нет старых — возвращаем всё новое
        if not old_list:
            # Чтобы не мутировать входящий new
            merged_doc = new.copy()
            merged_doc[self.root] = new_list.copy()
            return merged_doc

        # Если нет новых — возвращаем всё старое
        if not new_list:
            merged_doc = old.copy()
            merged_doc[self.root] = old_list.copy()
            return merged_doc

        # Подготовка эмбеддингов для semantic.match
        old_texts = [deep_get(o, self.key_path) or "" for o in old_list]
        new_texts = [deep_get(n, self.key_path) or "" for n in new_list]
        old_emb = self._model.encode(old_texts, convert_to_tensor=True, normalize_embeddings=True)
        new_emb = self._model.encode(new_texts, convert_to_tensor=True, normalize_embeddings=True)
        cos_scores = self._util.cos_sim(new_emb, old_emb)

        used_old = set()
        merged = []
        max_scores = float('-inf')

        for i, new_item in enumerate(new_list):
            # 1) точный матч по name
            match_idx: Optional[int] = None
            new_name = new_item.get("name", "")
            for j, old_item in enumerate(old_list):
                if id(old_item) not in used_old and old_item.get("name") == new_name:
                    match_idx = j
                    break

            # 2) семантический матч по key_path
            if match_idx is None:
                row = cos_scores[i]
                
                # Создаем маску для неиспользованных элементов
                unused_mask = [id(item) not in used_old for item in old_list]
                
                if any(unused_mask):
                    # 1) Собираем булевую маску "ещё не использовано"
                    mask = torch.tensor(unused_mask, device=row.device, dtype=torch.bool)

                    # 2) Клонируем row и «откидываем» все уже использованные, ставя им –∞
                    masked_row = row.clone()
                    masked_row[~mask] = float('-inf')
                    scores = masked_row.tolist() 
                    max_scores = max(scores)          # [float('-inf'), 0.9204]
                    j = scores.index(max_scores)           # найдёт 1
                    if scores[j] >= self.emb_threshold_comp:
                        match_idx = j

            # 3) merge / fallback
            if match_idx is not None:
                chosen = old_list[match_idx]
                used_old.add(id(chosen))
                merged.append(deep_merge_dicts(old_item, new_item))
            else:
                logger.info(f"Добавляем новый компонент {new_name}, max_score = {max_scores}")
                merged.append(new_item)

        # 4) добавляем все новые, которые не были использованы
        for old_item in old_list:
            if id(old_item) not in used_old:
                merged.append(old_item)

        # 5) собираем итоговый документ
        out = old.copy()
        out[self.root] = merged
        # остальные ключи из new (кроме root)
        for k, v in new.items():
            if k != self.root:
                out[k] = v

        return out



# Определяем каким полям каких моделей соответствуют вложенные BaseModel
def build_field_model_map(
    model_classes: Dict[str, Type[BaseModel]]
) -> Dict[Type[BaseModel], Dict[str, Type[BaseModel]]]:
    """
    Для Pydantic v2: ищем в model.model_fields → берем .annotation
    и находим поля, которые сами являются BaseModel или List[BaseModel].
    """
    mapping: Dict[Type[BaseModel], Dict[str, Type[BaseModel]]] = {}
    for cls in model_classes.values():
        if not inspect.isclass(cls) or not issubclass(cls, BaseModel):
            continue

        # В Pydantic2 поля в model_fields, в старой — в __fields__
        raw_fields = getattr(cls, "model_fields", getattr(cls, "__fields__", {}))
        field_map: Dict[str, Type[BaseModel]] = {}

        for name, field_info in raw_fields.items():
            # В v2 это FieldInfo, в v1 — ModelField, но у обоих есть .annotation
            tp = getattr(field_info, "annotation", None)
            if tp is None:
                continue

            origin = get_origin(tp)

            # 1) Прямой вложенный BaseModel
            if inspect.isclass(tp) and issubclass(tp, BaseModel):
                field_map[name] = tp

            # 2) List[SomeModel]
            elif origin is list or origin is list:
                args = get_args(tp)
                if args and inspect.isclass(args[0]) and issubclass(args[0], BaseModel):
                    field_map[name] = args[0]

        if field_map:
            mapping[cls] = field_map

    return mapping


models = {
    "Structural": Structural,
    "PublicInterface": PublicInterface,
    "Semantic": Semantic,
    "Component": Component,
    "ComponentsStructure": ComponentsStructure,
}

FIELD_MODEL_MAP = build_field_model_map(models)

class SemanticComponentMerger(SemanticYamlMerger):
    root_key = "components"
    root_model = Component
    field_model_map: ClassVar[Dict[Type[BaseModel], Dict[str, Type[BaseModel]]]] = FIELD_MODEL_MAP

    @crash_logger("semantic_components_merger")
    def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        # сохраняем старую версию для reference
        self._last_old = copy.deepcopy(old)
        merged = super().merge(old, new)
        return self.post_process(merged)

    def post_process(self, merged: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = []

        for comp in merged.get(self.root_key, []):
            if not isinstance(comp, dict) or "name" not in comp:
                logger.error(f"validation failed for '{comp.get('name')}'")
                continue

            name = comp["name"]
            comp_copy = copy.deepcopy(comp)

            # рекурсивно семантически «фиксить» все dict на пути
            self._semantic_fix(comp_copy, self.root_model)

            # валидация через Pydantic
            try:
                validated = self.root_model(**comp_copy)
            except ValidationError as e:
                logger.error(f"validation failed for '{name}': {e}")
                continue

            cleaned.append(validated.dict())

        out = copy.deepcopy(merged)
        out[self.root_key] = cleaned
        try:
            ComponentsStructure(**out)
        except ValidationError as e:
            logger.error(f"Top‐level validation failed: {e}")
        return out

    def _semantic_fix(
        self,
        cur: Dict[str, Any],
        model_cls: Type[BaseModel],
    ) -> None:
        """
        На данном уровне:
         1) находим extras = cur.keys() – model_cls.__fields__.keys()
         2) семантически мёржим каждый extra → best_field, если score >= emb_threshold
         3) иначе логим и удаляем
         4) рекурсивно спускаемся в дочерние dict/list по FIELD_MODEL_MAP
        """
        allowed = list(model_cls.__fields__.keys())
        incoming = list(cur.keys())
        extras = [k for k in incoming if k not in allowed]
        if extras and allowed:
            names = allowed + extras
            embs = self._model.encode(
                names,
                convert_to_tensor=True,
                normalize_embeddings=True
            )
            A = embs[: len(allowed)]
            E = embs[len(allowed) : ]
            sims = self._util.cos_sim(A, E)  # shape (len(allowed), len(extras))

            for j, extra in enumerate(extras):
                row = sims[:, j]
                best = int(row.argmax())
                score = float(row[best])
                if score >= self.emb_threshold_key:
                    target = allowed[best]
                    logger.info(
                        f"merging extra field '{extra}' → '{target}' (score {score:.2f})"
                    )
                    # если не dict или list — deep_merge умеет словари
                    if (not isinstance(cur[target], dict) or not isinstance(cur[extra], dict)):
                        cur[target] = cur[extra]
                    else: 
                        cur[target] = deep_merge_dicts(cur[target], cur[extra])
                else:
                    logger.error(
                        f"dropping field '{extra}' (no semantic match ≥ {self.emb_threshold_key})"
                    )
                # в любом случае удаляем обработанное поле
                cur.pop(extra, None)

        # рекурсивно «ниже» по дочерним моделям
        submap = self.field_model_map.get(model_cls, {})
        for field, sub_model in submap.items():
            val = cur.get(field)
            if isinstance(val, dict):
                self._semantic_fix(val, sub_model)
            elif isinstance(val, list):
                for idx, item in enumerate(val):
                    if isinstance(item, dict):
                        self._semantic_fix(item, sub_model)

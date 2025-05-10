# llm/toolkit/mergers/base.py
import yaml
import copy
from pydantic import ValidationError
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union, Set
from utils.deep_merge_dicts import deep_merge_dicts
from utils.merge_add_suffix import merge_add_suffix_recursive
from sentence_transformers import SentenceTransformer, util
from functools import lru_cache
from tests.crash_logger import crash_logger
from domain.models.components import Component, ComponentsStructure
from utils.logger import logger
from utils.deep_get import deep_get

class BaseMerger(ABC):
    @abstractmethod
    def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """
        :param old: разбранный из YAML/JSON словарь “старого” состояния
        :param new: разбранный из YAML/JSON словарь “нового” состояния
        :return: словарь с объединённым состоянием
        """
        ...


# class ComponentMerger(BaseMerger):
#     """
#     Алгоритм “старое + новое → merged” взят из OldComponentsParser.merge
#     Но тут мы работаем уже с Python-структурами, а не со строками.
#     """
#     @crash_logger("components_merge")
#     def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
#         old_comps: List[Dict[str, Any]] = old.get("components", [])
#         new_comps: List[Dict[str, Any]] = new.get("components", [])

#         # вспомогательные мапы
#         by_name = {c["name"]: c for c in old_comps if "name" in c}
#         used_new_ids = set()
#         merged: List[Dict[str, Any]] = []

#         for old_c in old_comps:
#             name = old_c.get("name")
#             match = None

#             # 1) точное совпадение
#             if name in by_name and any(n.get("name") == name for n in new_comps):
#                 match = next(n for n in new_comps if n.get("name") == name)
#                 used_new_ids.add(id(match))

#             # 2) семантическое совпадение (ваша логика)
#             if not match:
#                 resp = old_c.get("semantic", {}).get("responsibility")
#                 if resp:
#                     # ... ваш код с Levenshtein, threshold, best candidate ...
#                     from Levenshtein import ratio
#                     candidates = [
#                         n for n in new_comps
#                         if n.get("semantic", {}).get("responsibility") == resp
#                     ]
#                     best, best_ratio = None, 0.0
#                     for c in candidates:
#                         r = ratio(name or "", c.get("name",""))
#                         if r > best_ratio:
#                             best, best_ratio = c, r
#                     if best_ratio >= 0.8:
#                         match = best
#                         used_new_ids.add(id(best))

#             # 3) объединяем
#             if match:
#                 merged.append(deep_merge_dicts(old_c, match))
#             else:
#                 merged.append(old_c)

#         # 4) добавляем все новые, что не попали
#         for nc in new_comps:
#             if id(nc) not in used_new_ids:
#                 merged.append(nc)

#         # 5) финальный документ
#         out = old.copy()
#         # перезаписываем прочие поля из new, кроме components
#         for k, v in new.items():
#             if k != "components":
#                 out[k] = v
#         out["components"] = merged
#         return out


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
        emb_threshold: float = 0.75,
    ):
        self.root = root
        self.key_path = key_path
        self.emb_threshold = emb_threshold
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
        cos_scores = util.cos_sim(old_emb, new_emb)

        used_new = set()
        merged = []

        for i, old_item in enumerate(old_list):
            # 1) точный матч по name
            match_idx: Optional[int] = None
            old_name = old_item.get("name", "")
            for j, new_item in enumerate(new_list):
                if id(new_item) not in used_new and new_item.get("name") == old_name:
                    match_idx = j
                    break

            # 2) семантический матч по key_path
            if match_idx is None:
                row = cos_scores[i]
                # индекс лучшей новой записи
                j = int(row.argmax())
                if float(row[j]) >= self.emb_threshold and id(new_list[j]) not in used_new:
                    match_idx = j

            # 3) merge / fallback
            if match_idx is not None:
                chosen = new_list[match_idx]
                used_new.add(id(chosen))
                merged.append(deep_merge_dicts(old_item, chosen))
            else:
                merged.append(old_item)

        # 4) добавляем все новые, которые не были использованы
        for new_item in new_list:
            if id(new_item) not in used_new:
                merged.append(new_item)

        # 5) собираем итоговый документ
        out = old.copy()
        out[self.root] = merged
        # остальные ключи из new (кроме root)
        for k, v in new.items():
            if k != self.root:
                out[k] = v

        return out

    
class SemanticComponentMerger(SemanticYamlMerger):
    """
    Семантический мердж новых полей в существующие, без хардкода field_add suffix’ов.
    Для каждого неизвестного ключа в компоненте находим наиболее похожий «разрешённый»
    (по названию ключа), и если косинусная близость > emb_threshold, то deep-merge’им
    значения внутрь этого ключа.
    """
    def __init__(
        self,
        root: str = "components",
        key_path: str = "semantic.responsibility",
        model_name: str = "all-MiniLM-L6-v2",
        emb_threshold: float = 0.75,
    ):
        super().__init__(root=root, key_path=key_path,
                         model_name=model_name, emb_threshold=emb_threshold)
        # допустимые поля на верхнем уровне одного компонента
        self.allowed_keys: List[str] = list(Component.__fields__.keys())

    @crash_logger("semantic_component_merger")
    def merge(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        return super().merge(old,new)

    def post_process(self, result: Dict[str, Any]) -> Dict[str, Any]:
        merged = super().merge(result, result)  # semantic merge основной логики
        cleaned: List[Dict[str, Any]] = []

        for comp in merged.get(self.root, []):
            if not isinstance(comp, dict):
                logger.error(f"[SemanticComponentMerger] skip non-dict: {comp!r}")
                continue

            comp = copy.deepcopy(comp)
            incoming: Set[str] = set(comp.keys())
            extras = incoming - set(self.allowed_keys)
            if extras:
                # закодируем все field names
                all_keys = self.allowed_keys + list(extras)
                embeds = self._model.encode(all_keys, convert_to_tensor=True, normalize_embeddings=True)
                # матрица similarities: allowed_keys × extras
                A = embeds[: len(self.allowed_keys)]
                E = embeds[len(self.allowed_keys):]
                sims = util.cos_sim(A, E)  # shape [len(allowed), len(extras)]

                # для каждого extra – найдём best match в allowed
                for j, extra in enumerate(extras):
                    row = sims[:, j]
                    best_idx = int(row.argmax())
                    score = float(row[best_idx])

                    if score >= self.emb_threshold:
                        target = self.allowed_keys[best_idx]
                        logger.info(
                            f"[SemanticComponentMerger] merging extra field '{extra}' "
                            f"→ '{target}' (score {score:.2f})"
                        )
                        # deep-merge comp[extra] into comp[target]
                        comp.setdefault(target, {})
                        comp[target] = deep_merge_dicts(comp[target], comp[extra])
                    else:
                        logger.error(
                            f"[SemanticComponentMerger] dropping unknown field '{extra}' "
                            f"(no match above threshold {self.emb_threshold})"
                        )
                    # убираем processed extra
                    comp.pop(extra, None)

            # теперь валидируем
            try:
                validated = Component(**comp)
            except ValidationError as e:
                logger.error(f"[SemanticComponentMerger] validation failed for '{comp.get('name')}' : {e}")
                continue

            cleaned.append(validated.dict())

        # финальный документ
        out = {**merged, self.root: cleaned}
        # проверка всего документа
        try:
            ComponentsStructure(**out)
        except ValidationError as e:
            logger.error(f"[SemanticComponentMerger] top-level validation error: {e}")

        return out

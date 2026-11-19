"""Вероятность, что сводный эксперт упомянет расхождение по критерию в своём комментарии.

С июля 2026 сводные эксперты пишут комментарии по критериям («2.1.5. Название критерия: замечание»), поэтому по ним
можно разметить, какие из найденных нами расхождений эксперт считает замечанием, а какие нет (``scripts/fit_primary_mention.py``).
Логистическая регрессия: признаки — ``вид:ключ`` расхождения, число расхождений в заключении и вид расхождения. Кросс-валидация
по заключениям (закупки): AUC 0,76; при вероятности ≥ 0,4 доля упомянутых расхождений 58% против 42% без отбора, при этом
остаются 79% упомянутых критериев. Вероятность не влияет на оценку ``range``; она нужна, чтобы в комментарий попадали
расхождения, которые эксперт, скорее всего, отметил бы сам, а остальные оставались в ``differences``.

Файл ``primary_mention_model.json``: по группе ``weights`` (``вид:ключ`` → вес), ``bias``, ``n_coef`` (при числе расхождений / 20),
``kind_coef`` (по виду), ``min_probability`` (порог отбора в комментарий).
"""
import json
import math
import os
from typing import Any, Dict, List, Optional

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "primary_mention_model.json")
MAX_DIFFERENCES = 40            # число расхождений в признаке обрезается (как при обучении)
_model_cache: Optional[Dict[str, Any]] = None


def load_model(path: str = MODEL_PATH) -> Dict[str, Any]:
    """Читает модель (кэшируется); пустой словарь, если файла нет."""
    global _model_cache
    if path != MODEL_PATH:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    if _model_cache is None:
        try:
            with open(path, encoding="utf-8") as fh:
                _model_cache = json.load(fh)
        except (OSError, ValueError):
            _model_cache = {}
    return _model_cache


def probability(spec: Dict[str, Any], kind: str, key: str, n_differences: int) -> float:
    """Вероятность упоминания расхождения ``kind:key`` при ``n_differences`` расхождениях в заключении."""
    z = (float(spec["bias"]) + float(spec["weights"].get(f"{kind}:{key}", 0.0))
         + float(spec["n_coef"]) * min(n_differences, MAX_DIFFERENCES) / 20
         + float(spec["kind_coef"].get(kind, 0.0)))
    return 1.0 / (1.0 + math.exp(-z))


def probabilities(group: str, differences: List[Any], model: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Вероятности упоминания по ключам расхождений (``Difference``); пусто, если модели группы нет.

    Args:
        group: ``procurement`` или ``report`` (для отчётных моделей нет: комментарии строятся по названиям полей).
        differences: Расхождения заключения (:attr:`primary_compare.Comparison.differences`).
        model: Параметры модели (по умолчанию из файла).

    Returns:
        dict: ``ключ критерия → вероятность``.
    """
    spec = (model if model is not None else load_model()).get(group)
    if not spec or not differences:
        return {}
    return {d.key: round(probability(spec, d.kind, d.key, len(differences)), 3) for d in differences}


def select(differences: List[Any], probs: Dict[str, float], min_probability: float, keep_top: int = 3) -> List[Any]:
    """Расхождения для комментария: вероятность не ниже порога; если таких нет — ``keep_top`` самых вероятных.

    Без вероятностей (нет модели) возвращает все расхождения.
    """
    if not probs:
        return list(differences)
    chosen = [d for d in differences if probs.get(d.key, 1.0) >= min_probability]
    if chosen or not differences:
        return chosen
    return sorted(differences, key=lambda d: -probs.get(d.key, 0.0))[:keep_top]

"""Взвешенный процент сходства первичного заключения с итоговым и оценка ``range`` по шкале порогов.

Схема (вместо многоклассовой модели): каждое расхождение с итоговым заключением «снимает» часть процента — тем больше,
чем тяжелее для сводного эксперта этот критерий в этом виде расхождения (вес выучен по истории оценок, см.
``scripts/fit_primary_score.py``). Если все веса равны, процент совпадает с :attr:`primary_compare.Comparison.match_percent`.
Итоговый процент переводится в оценку по порогам, подобранным по истории: ниже ``t0`` — 0, до ``t1`` — 0.5, до ``t2`` —
0.75, иначе 1.0. Оценка объяснима: для каждого расхождения известно, сколько процентов оно сняло.

Файл модели ``primary_score_model.json`` содержит по группам (``procurement`` / ``report``): ``weights`` (``вид:ключ``
→ вес), ``w_avg`` (средний положительный вес — нормировка), ``w_default`` (вес расхождения, которого нет в обучении),
``thresholds`` (три порога в процентах).
"""
import json
import os
from typing import Any, Dict, List, Optional

from knowledge_store.primary_compare import Comparison

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "primary_score_model.json")
GRADES = (0.0, 0.5, 0.75, 1.0)
_model_cache: Optional[Dict[str, Any]] = None


def load_model(path: str = MODEL_PATH) -> Dict[str, Any]:
    """Читает веса и пороги (кэшируется); пустой словарь, если файла нет."""
    global _model_cache
    if _model_cache is None or path != MODEL_PATH:
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            loaded = {}
        if path != MODEL_PATH:
            return loaded
        _model_cache = loaded
    return _model_cache


def difference_weight(spec: Dict[str, Any], kind: str, key: str) -> float:
    """Вес расхождения: выученный для ``вид:ключ``, иначе средний ``w_default`` группы."""
    return float(spec["weights"].get(f"{kind}:{key}", spec["w_default"]))


def weighted_percent(cmp: Comparison, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Взвешенный процент сходства и вычеты по расхождениям.

    Процент = ``100 · (1 − Σвесов расхождений / (общих критериев · w_avg))``, обрезанный до 0–100. Вес может быть
    отрицательным (расхождение, которое оценивающие обычно прощают): тогда вычет отрицательный, то есть добавляет процент.

    Args:
        cmp: Результат :func:`primary_compare.compare`.
        spec: Параметры группы из :func:`load_model`.

    Returns:
        dict: ``percent`` и ``deductions`` — ``[{"key", "kind", "percent"}]`` по убыванию вычета (процентные пункты; отрицательный — добавил);
        ``percent`` равен ``None``, если общих критериев нет.
    """
    if not cmp.common:
        return {"percent": None, "deductions": []}
    scale = cmp.common * float(spec["w_avg"])
    deductions = [{"key": d.key, "kind": d.kind, "percent": round(100 * difference_weight(spec, d.kind, d.key) / scale, 1)}
                  for d in cmp.differences]
    deductions.sort(key=lambda x: -x["percent"])
    total = sum(x["percent"] for x in deductions)
    return {"percent": round(min(100.0, max(0.0, 100 - total)), 1), "deductions": deductions}


def grade_by_percent(percent: Optional[float], spec: Dict[str, Any]) -> Optional[float]:
    """Оценка по порогам процента: ``< t0`` → 0, ``< t1`` → 0.5, ``< t2`` → 0.75, иначе 1.0."""
    if percent is None:
        return None
    t = spec["thresholds"]
    return GRADES[sum(1 for x in t if percent >= x)]


def neighbour_grade(percent: Optional[float], spec: Dict[str, Any], margin: float) -> Optional[float]:
    """Соседняя оценка, если процент отстоит от ближайшего порога не больше чем на ``margin`` п. п.; иначе ``None``.

    Нужна, чтобы показать сводному эксперту, что оценка пограничная (вторая оценка — в сторону порога).
    """
    if percent is None:
        return None
    t = spec["thresholds"]
    idx = sum(1 for x in t if percent >= x)
    candidates: List[tuple] = []
    if idx > 0:
        candidates.append((percent - t[idx - 1], GRADES[idx - 1]))
    if idx < len(t):
        candidates.append((t[idx] - percent, GRADES[idx + 1]))
    gap, grade = min(candidates)
    return grade if gap <= margin else None


def evaluate(cmp: Comparison, model: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Полный результат для группы случая: процент, вычеты, оценка по порогам.

    Returns:
        dict | None: ``{"percent", "deductions", "range", "spec"}``; ``None`` — модели группы нет или нет общих критериев.
    """
    spec = (model if model is not None else load_model()).get(cmp.group)
    if not spec:
        return None
    result = weighted_percent(cmp, spec)
    if result["percent"] is None:
        return None
    return {**result, "range": grade_by_percent(result["percent"], spec), "spec": spec}

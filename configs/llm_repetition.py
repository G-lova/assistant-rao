"""Защита от зацикливания модели при извлечении данных: повторы записей и обрезанный ответ.

Модель иногда повторяет одну и ту же запись списка (``finances``, ``dates`` …) до лимита ответа: ответ
обрезается, JSON восстанавливается ``repair_json``, а всё, что модель не успела дописать (организации, нормы,
предмет), теряется (20.10.2026: сумма 1 799 686,68 повторена ~20 раз в «Обосновании НМЦК»).

* :func:`dedupe_repeats` убирает повторы записей в списках ``raw_data`` (сравнение без ``confidence``);
* если ответ был обрезан и повторов не меньше ``LOOP_MIN`` — это зацикливание: вызывающий повторяет запрос
  со штрафом за повторы (:data:`PENALTY_KWARGS`).
"""
import json
from typing import Any, Dict, Tuple

LOOP_MIN = 5                    # повторов в обрезанном ответе — признак зацикливания
# Параметры повторного запроса: штраф за повторяющиеся токены (OpenAI-совместимый и vLLM)
PENALTY_KWARGS = {"frequency_penalty": 0.4}
PENALTY_EXTRA_BODY = {"repetition_penalty": 1.1}


def _signature(item: Any) -> str:
    """Подпись записи для сравнения: без полей ``confidence`` на всех уровнях."""
    def strip(v: Any) -> Any:
        """Рекурсивно убирает ``confidence``."""
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k != "confidence"}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return json.dumps(strip(item), ensure_ascii=False, sort_keys=True, default=str)


def dedupe_repeats(result: Any) -> Tuple[Any, int]:
    """Убирает повторы записей в списках ``raw_data`` (первое вхождение остаётся).

    Args:
        result: Ответ экстрактора (словарь с ``raw_data`` или сам ``raw_data``).

    Returns:
        Tuple[Any, int]: Тот же объект без повторов и число удалённых записей.
    """
    if not isinstance(result, dict):
        return result, 0
    raw = result.get("raw_data") if isinstance(result.get("raw_data"), dict) else result
    removed = 0
    for key, value in list(raw.items()):
        if not isinstance(value, list):
            continue
        seen, kept = set(), []
        for item in value:
            sig = _signature(item)
            if sig in seen:
                removed += 1
                continue
            seen.add(sig)
            kept.append(item)
        raw[key] = kept
    return result, removed


def penalized(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Параметры ``chat.completions.create`` со штрафом за повторы (``extra_body`` дополняется)."""
    out = {**kwargs, **PENALTY_KWARGS}
    out["extra_body"] = {**(kwargs.get("extra_body") or {}), **PENALTY_EXTRA_BODY}
    return out

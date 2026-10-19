"""Формат значений сводного заключения: как у экспертов в выгрузке (``true/false`` или 1/0/2).

Внутри сборки (:mod:`knowledge_store.summary`) значения критериев — числа 1/0/2 (в наличии/соответствует,
отсутствует/не соответствует, не предусмотрено/не применимо). В формах аукциона, котировок и единственного поставщика
эксперты хранят большинство критериев разделов 2–4 как ``true``/``false`` (список полей — ``value_formats.json``,
строится :mod:`scripts.derive_value_format` по выгрузкам). Перед сохранением заключения числа переводятся в тот формат,
который ожидает форма. Для форм без записи в справочнике (конкурс, 223-ФЗ — пока нет выгрузок) значения остаются числами.
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

FORMATS_PATH = Path(__file__).resolve().parent / "value_formats.json"


@lru_cache(maxsize=1)
def _formats() -> Dict[str, dict]:
    """Справочник форматов из ``value_formats.json`` (пустой, если файла нет)."""
    try:
        return json.loads(FORMATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def bool_keys(form_code: Optional[str]) -> FrozenSet[str]:
    """Ключи формы, значения которых хранятся как ``true``/``false``."""
    return frozenset((_formats().get(form_code or "") or {}).get("bool", ()))


def to_form_format(form_code: Optional[str], data: Dict[str, Any], trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Переводит числовые значения 1/0 в ``true``/``false`` для булевых полей формы.

    Значение «2» («не предусмотрено / не применимо») булевым не выражается: в таких полях эксперты его не используют,
    поэтому оно заменяется пустым значением (остаётся эксперту), а в ``trace`` добавляется пояснение.

    Args:
        form_code: Код формы.
        data: Значения полей (числа 1/0/2, ``None``).
        trace: Доказательства; для замененных значений дописывается ``note`` (изменяется на месте).

    Returns:
        dict: Новый словарь ``data``; исходный не изменяется.
    """
    keys = bool_keys(form_code)
    if not keys:
        return dict(data)
    result = dict(data)
    for key in keys:
        value = result.get(key)
        if value is None or isinstance(value, bool):
            continue
        if value in (0, 1):
            result[key] = bool(value)
        else:
            result[key] = None
            if trace is not None and isinstance(trace.get(key), dict):
                trace[key]["note"] = (str(trace[key].get("note") or "") +
                                      " Значение «2» в булевом поле формы не выражается: оставлено эксперту.").strip()
    return result

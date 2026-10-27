"""Приведение полей ответа модели к ожидаемым типам при объединении блоков извлечения.

Модель иногда отдаёт строку или список вместо объекта (``"document_name": "Контракт"``, ``"evidence": "фрагмент"``),
особенно в обрезанном и восстановленном JSON. Объединение блоков падало с «'str' object has no attribute 'get'», и
терялись данные всего документа (прогоны 02.07 и 06.07, 23–26.10.2026). Используется в ``data_extractor`` и
``type_data_extractor``.
"""
from typing import Any, Dict, Optional


def as_obj(value: Any, key: str) -> Dict[str, Any]:
    """Поле ответа модели как объект.

    Args:
        value: Значение поля.
        key: Ключ, под которым скалярное значение кладётся в объект (``value`` / ``method`` / ``status``).

    Returns:
        Dict[str, Any]: Объект поля; пустой, если значения нет или оно не приводится.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, int, float)) and value != "":
        return {key: value}
    return {}


def as_evidence(value: Any) -> Optional[Dict[str, Any]]:
    """Доказательство (``evidence``) как объект: строка — ``{"fragment": …}``, список — первый объект или
    фрагменты через «; »; пусто — ``None``."""
    if not value:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        first = next((v for v in value if isinstance(v, dict)), None)
        if first is not None:
            return first
        text = "; ".join(str(v) for v in value if v)
        return {"fragment": text} if text else None
    return {"fragment": str(value)}


def as_text(value: Any) -> str:
    """Текстовое поле (``summary``) как строка: список — через пробел, прочее — ``str``."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v)
    return value if isinstance(value, str) else str(value)

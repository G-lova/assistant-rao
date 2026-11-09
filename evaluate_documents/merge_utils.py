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


def _norm_context(value: Any) -> str:
    """Контекст для сравнения: нижний регистр, без знаков препинания и лишних пробелов."""
    import re
    return re.sub(r"[^\wа-яё]+", " ", str(value or "").lower()).strip()


def dedupe_raw_data(raw: Dict[str, Any]) -> int:
    """Убирает дубли после объединения блоков (одно и то же значение, извлечённое из нескольких блоков).

    * суммы — одинаковое значение и вид (``kind``), а без вида — одинаковый контекст;
    * даты — одинаковая дата (первые 10 символов) и вид / контекст;
    * стороны — одинаковый ИНН и роль (у записи без ИНН — имя и роль); у оставшейся заполняются пустые реквизиты
      из дубля.

    Args:
        raw: ``raw_data`` (меняется на месте).

    Returns:
        int: Сколько записей удалено.
    """
    if not isinstance(raw, dict):
        return 0
    removed = 0

    def key_amount(item):
        """Ключ суммы."""
        try:
            value = round(float(item.get("value")), 2)
        except (TypeError, ValueError):
            value = str(item.get("value"))
        return value, item.get("kind") or _norm_context(item.get("context"))

    def key_date(item):
        """Ключ даты."""
        return str(item.get("value"))[:10], item.get("kind") or _norm_context(item.get("context"))

    for field, key in (("finances", key_amount), ("dates", key_date)):
        items = raw.get(field)
        if not isinstance(items, list):
            continue
        seen, kept = set(), []
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            k = key(item)
            if k in seen:
                removed += 1
                continue
            seen.add(k)
            kept.append(item)
        raw[field] = kept

    orgs = raw.get("organizations")
    if isinstance(orgs, list):
        index: Dict[Any, Dict[str, Any]] = {}
        kept = []
        for org in orgs:
            if not isinstance(org, dict):
                kept.append(org)
                continue
            inn = "".join(ch for ch in str(org.get("inn") or "") if ch.isdigit())
            k = (inn, _norm_context(org.get("role"))) if inn else (_norm_context(org.get("name")), _norm_context(org.get("role")))
            if k in index:
                target = index[k]
                for field in ("name", "inn", "kpp", "ogrn", "address"):
                    if org.get(field) and not target.get(field):
                        target[field] = org[field]
                removed += 1
                continue
            index[k] = org
            kept.append(org)
        raw["organizations"] = kept
    return removed


def chunk_counts(raw: Dict[str, Any], keys: tuple) -> str:
    """Что пришло из блока по полям — для журнала объединения: «finances: 3; dates: 2» / «contract_number: ЗА-8».

    Args:
        raw: ``raw_data`` одного блока.
        keys: Поля.

    Returns:
        str: Краткая сводка; «нет» для пустых полей.
    """
    parts = []
    for key in keys:
        value = (raw or {}).get(key)
        if isinstance(value, list):
            parts.append(f"{key}: {len(value)}")
        elif isinstance(value, dict):
            shown = value.get("value") or value.get("method") or value.get("description") or value.get("address")
            parts.append(f"{key}: {str(shown)[:80] if shown else 'нет'}")
        elif value:
            parts.append(f"{key}: {str(value)[:80]}")
        else:
            parts.append(f"{key}: нет")
    return "; ".join(parts)

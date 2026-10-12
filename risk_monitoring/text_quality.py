"""Контроль качества извлечённого текста (OCR) до анализа ИИ.

VL-модель распознавания иногда «зацикливается» и выдаёт сотни повторов одного фрагмента
(«2,44 2,44 2,44 …») или домысливает текст. Анализировать такой текст бессмысленно:
модель находит «противоречия» и «ошибки» в мусоре. Модуль:

* находит циклы повторов (фрагмент из 1–4 слов, повторённый подряд 10 и более раз) и схлопывает их;
* оценивает долю текста в циклах и объём осмысленного текста;
* выносит вердикт: ``ok`` — анализировать; ``suspect`` — анализировать очищенный текст с пометкой;
  ``bad`` — не анализировать (риски и паспорт по такому тексту не строятся).

Чистые функции без зависимостей — их покрывают тесты ``tests/test_rm_quality.py``.
"""
import re
from typing import Any, Dict, Tuple

LOOP_RE = re.compile(r"(?<!\S)(\S+(?:[ \t]+\S+){0,3}?)(?:\s+\1(?!\S)){9,}")
LETTERS_RE = re.compile(r"[А-Яа-яЁёA-Za-z]")

BAD_LOOP_SHARE = 0.3        # больше 30 % текста в циклах — текст негоден
MIN_LETTERS = 200           # меньше 200 букв после очистки — анализировать нечего
MAX_STRING = 2000           # предел длины строки в ai_analysis


def collapse_loops(text: str) -> Tuple[str, int, int]:
    """Схлопывает циклы повторов до одного вхождения с пометкой.

    Args:
        text: Исходный текст.

    Returns:
        Tuple[str, int, int]: Очищенный текст, число циклов, число символов, попавших в циклы.
    """
    loops = 0
    removed = 0

    def repl(m: "re.Match") -> str:
        """Заменяет цикл одним вхождением и пометкой о числе повторов."""
        nonlocal loops, removed
        unit = m.group(1)
        count = len(re.findall(re.escape(unit), m.group(0)))
        loops += 1
        removed += len(m.group(0)) - len(unit)
        return f"{unit} [повтор ×{count}]"

    cleaned = LOOP_RE.sub(repl, text or "")
    return cleaned, loops, removed


def assess(text: str) -> Dict[str, Any]:
    """Оценка качества текста документа.

    Args:
        text: Текст после чтения файла (в т. ч. OCR).

    Returns:
        Dict[str, Any]: ``status`` (``ok`` / ``suspect`` / ``bad``), ``issues``, ``loops``,
        ``loop_share`` (доля символов в циклах), ``letters`` (букв после очистки), ``text`` (очищенный текст).
    """
    text = text or ""
    cleaned, loops, removed = collapse_loops(text)
    share = round(removed / len(text), 4) if text else 0.0
    letters = len(LETTERS_RE.findall(cleaned))
    issues = []
    if loops:
        issues.append(f"Циклы повторов в распознанном тексте: {loops} (≈{share:.0%} текста) — вероятен сбой OCR")
    if letters < MIN_LETTERS:
        issues.append(f"Мало осмысленного текста: {letters} букв")
    if share >= BAD_LOOP_SHARE or letters < MIN_LETTERS:
        status = "bad"
    elif loops:
        status = "suspect"
    else:
        status = "ok"
    return {"status": status, "issues": issues, "loops": loops, "loop_share": share, "letters": letters,
            "text": cleaned}


def clean_value(value: Any, max_len: int = MAX_STRING) -> Any:
    """Очищает значения для ``ai_analysis``: схлопывает циклы повторов и ограничивает длину строк.

    Args:
        value: Строка, список, словарь или скаляр.
        max_len: Предельная длина строки.

    Returns:
        Any: Очищенная копия.
    """
    if isinstance(value, str):
        cleaned, _, _ = collapse_loops(value)
        return cleaned if len(cleaned) <= max_len else cleaned[:max_len] + "…"
    if isinstance(value, list):
        return [clean_value(v, max_len) for v in value]
    if isinstance(value, dict):
        return {k: clean_value(v, max_len) for k, v in value.items()}
    return value


def digits(value: Any) -> str:
    """Только цифры из значения (для сверки ИНН, КПП, ОГРН, номеров с текстом)."""
    return re.sub(r"\D", "", str(value or ""))


def verify_identifiers(raw_data: Dict[str, Any], text: str) -> Tuple[Dict[str, Any], list]:
    """Убирает из извлечённых данных реквизиты, которых нет в тексте документа.

    Модель иногда «дописывает» ИНН, КПП, ОГРН и номера (например, ``7801000000``). Реквизит
    сохраняется, только если его цифры встречаются в тексте.

    Args:
        raw_data: ``raw_data`` из ``DataExtractor``.
        text: Текст документа.

    Returns:
        Tuple[Dict[str, Any], list]: Копия ``raw_data`` и список снятых реквизитов (для ``issues``).
    """
    text_digits = digits(text)
    removed = []
    raw = dict(raw_data or {})

    def present(value: Any) -> bool:
        """Есть ли цифры реквизита в тексте (короткие значения не проверяются)."""
        d = digits(value)
        return len(d) < 5 or d in text_digits

    orgs = []
    for org in raw.get("organizations") or []:
        if not isinstance(org, dict):
            continue
        org = dict(org)
        for key in ("inn", "kpp", "ogrn"):
            if org.get(key) and not present(org[key]):
                removed.append(f"{key.upper()} {org[key]} ({org.get('name') or 'организация'})")
                org[key] = None
        orgs.append(org)
    if "organizations" in raw:
        raw["organizations"] = orgs
    cn = raw.get("contract_number")
    if isinstance(cn, dict) and cn.get("value") and not present(cn["value"]):
        removed.append(f"номер {cn['value']}")
        raw["contract_number"] = {**cn, "value": None}
    return raw, removed

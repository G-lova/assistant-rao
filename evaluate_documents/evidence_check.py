"""Проверка извлечённых данных по тексту документа: значения, которых в документе нет, отбрасываются.

Модель извлечения (``TypeDataExtractor``) иногда «находит» суммы, даты, номера и реквизиты, которых в документе нет,
и подкрепляет их выдуманным фрагментом (экспертиза 7185: «НМЦК: 1 500 000,00 руб.» в договоре, где НМЦК не указана;
ИНН 1410000000; дата подписания 2023-10-15 у договора 2025 года). Такие значения потом становятся «противоречиями»
в итоговом заключении.

Проверка детерминированная и осторожная: значение удаляется, только если его нет в тексте ни в одной из обычных
записей (с пробелами-разделителями разрядов, запятой или точкой, «млн»/«тыс», дата цифрами или словами).
Проверяются суммы, даты, номер договора, ИНН/КПП/ОГРН и метод обоснования НМЦК; остальные поля не трогаются.
"""
import copy
import re
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from configs.logger import get_logger

logger = get_logger(__name__)

# Символы, которые OCR и люди путают в номерах: кириллица ↔ латиница, буква ↔ цифра
_CONFUSABLES = str.maketrans({
    "З": "3", "з": "3", "О": "0", "о": "0", "O": "0", "o": "0",
    "А": "A", "а": "A", "В": "B", "в": "B", "Е": "E", "е": "E", "К": "K", "к": "K", "М": "M", "м": "M",
    "Н": "H", "н": "H", "Р": "P", "р": "P", "С": "C", "с": "C", "Т": "T", "т": "T", "Х": "X", "х": "X",
    "У": "Y", "у": "Y", "І": "I", "l": "1", "I": "1", "|": "1",
})

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_RE = r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"

# Метод обоснования НМЦК → основы слов, которые должны встретиться в тексте
NMCK_METHOD_STEMS = {
    "рыночн": ("рыночн", "сопоставим"),
    "сопоставим": ("рыночн", "сопоставим"),
    "смет": ("смет",),
    "тариф": ("тариф",),
    "затрат": ("затрат",),
    "норматив": ("норматив",),
    "параметр": ("параметр",),
}


def normalize_number(value: Any) -> str:
    """Приводит номер договора к виду для сравнения: без пробелов, кавычек и «№», путаемые символы — к одному.

    ``ЗА-8/487/25`` и ``3А-8/487/25`` (OCR прочитал «З» как «3») дают одну и ту же строку.

    Args:
        value: Номер (строка или число).

    Returns:
        str: Нормализованный номер (пустая строка для пустого значения).
    """
    if value is None:
        return ""
    s = str(value).translate(_CONFUSABLES).upper()
    s = re.sub(r"[\s№#\"'«»]", "", s)
    s = s.replace("—", "-").replace("–", "-")
    return s


def _numbers_text(text: str) -> str:
    """Склеивает разряды чисел: ``33 728 698,15`` → ``33728698,15``, ``2076618-96`` → ``2076618,96``."""
    t = text.replace(" ", " ").replace(" ", " ")
    t = re.sub(r"(?<=\d)[ '’](?=\d{3}(?!\d))", "", t)
    t = re.sub(r"(?<=\d)-(?=\d{2}(?!\d))", ",", t)
    return t


def text_numbers(text: str) -> Set[float]:
    """Все числа текста (с учётом разрядов, «млн», «тыс», «млрд»), округлённые до копеек.

    Args:
        text: Текст документа.

    Returns:
        set[float]: Числа.
    """
    found: Set[float] = set()
    t = _numbers_text(text)
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(млрд|млн|тыс)?", t, flags=re.I):
        try:
            num = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        found.add(round(num, 2))
        mult = (m.group(2) or "").lower()
        if mult:
            found.add(round(num * {"млрд": 1e9, "млн": 1e6, "тыс": 1e3}[mult], 2))
    # «33 728 698 (Тридцать три миллиона …) рублей 15 копеек» — копейки отдельно от рублей
    for m in re.finditer(r"(\d+)\s*(?:\([^)]{0,300}\))?\s*руб\w*\.?\s*(\d{1,2})\s*коп", t, flags=re.I):
        found.add(round(int(m.group(1)) + int(m.group(2)) / 100, 2))
    return found


def _to_float(value: Any) -> Optional[float]:
    """Сумма из значения модели (число или строка ``33 728 698,15``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = _numbers_text(value).replace(" ", "")
        m = re.search(r"\d+(?:[.,]\d+)?", s)
        if m:
            try:
                return float(m.group(0).replace(",", "."))
            except ValueError:
                return None
    return None


def amount_in_text(value: Any, numbers: Set[float]) -> bool:
    """Есть ли сумма в тексте.

    Args:
        value: Сумма из извлечённых данных.
        numbers: Числа текста (:func:`text_numbers`).

    Returns:
        bool: ``True``, если значение есть в тексте или его не удалось разобрать (тогда не трогаем).
    """
    num = _to_float(value)
    if num is None:
        return True
    return any(abs(num - n) <= 0.011 for n in numbers)


def _year(y: str) -> int:
    """Год из 2 или 4 цифр."""
    n = int(y)
    return 2000 + n if n < 100 else n


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    """Дата или ``None`` для невозможных значений."""
    try:
        return date(y, m, d)
    except ValueError:
        return None


def text_dates(text: str) -> Set[date]:
    """Все даты текста: ``15.09.2025``, ``15 09 25``, ``2025-09-15``, ``«02» сентября 2025``, ``15 сентября 2025 г.``.

    Args:
        text: Текст документа.

    Returns:
        set[date]: Даты.
    """
    found: Set[date] = set()
    low = text.lower()
    for m in re.finditer(r"(?<!\d)(\d{1,2})\D{1,3}(\d{1,2})\D{1,3}(\d{4}|\d{2})(?!\d)", low):
        dt = _safe_date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
        if dt:
            found.add(dt)
    for m in re.finditer(r"(?<!\d)(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})(?!\d)", low):
        dt = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            found.add(dt)
    for m in re.finditer(r"(?<!\d)(\d{1,2})\D{0,3}\s*" + _MONTH_RE + r"\s*(\d{4})", low):
        month = next(v for k, v in MONTHS.items() if m.group(2).startswith(k))
        dt = _safe_date(int(m.group(3)), month, int(m.group(1)))
        if dt:
            found.add(dt)
    return found


def parse_date(value: Any) -> Optional[date]:
    """Дата из значения модели (``2025-09-15``, ``15.09.2025``); ``None`` — не дата или не разобрана."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?", s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})(?:\s*г\.?)?", s)
    if m:
        return _safe_date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def date_in_text(value: Any, dates: Set[date]) -> bool:
    """Есть ли дата в тексте; значение, которое не похоже на дату, считается подтверждённым."""
    dt = parse_date(value)
    return True if dt is None else dt in dates


def _digits(s: Any) -> str:
    """Только цифры строки."""
    return re.sub(r"\D", "", str(s or ""))


class _TextIndex:
    """Предвычисленные представления текста документа для проверок."""

    def __init__(self, text: str):
        """Строит индекс.

        Args:
            text: Текст документа.
        """
        self.text = text or ""
        self.low = self.text.lower()
        self.numbers = text_numbers(self.text)
        self.dates = text_dates(self.text)
        self.digits = _digits(self.text)
        self.norm = normalize_number(self.text)

    def has_number_id(self, value: Any) -> bool:
        """ИНН/КПП/ОГРН есть в тексте (сравнение по цифрам, без пробелов)."""
        d = _digits(value)
        return not d or d in self.digits

    def has_contract_number(self, value: Any) -> bool:
        """Номер договора есть в тексте (с учётом путаемых символов)."""
        n = normalize_number(value)
        return not n or n in self.norm


def plausible_contract_number(value: Any) -> bool:
    """Похоже ли значение на номер договора: есть цифра, это не сумма и не дата, не слишком длинное.

    Модель иногда кладёт в номер договора сумму («124,92 тыс.руб.») или дату.

    Args:
        value: Значение ``contract_number.value``.

    Returns:
        bool: ``True`` — похоже на номер.
    """
    s = str(value or "").strip()
    if not s or len(s) > 60 or not re.search(r"\d", s):
        return False
    if re.search(r"руб|коп|тыс|млн|млрд|%|₽", s, flags=re.I):
        return False
    if parse_date(s) is not None:
        return False
    return not re.fullmatch(r"[\d\s]+[.,]\d{1,2}", s)   # «124,92» — сумма, а не номер


def verify_raw_data(raw: Dict[str, Any], text: str,
                    expected_number: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Убирает из ``raw_data`` значения, которых нет в тексте документа.

    Args:
        raw: ``raw_data`` результата извлечения.
        text: Полный текст документа.
        expected_number: Номер договора экспертизы. Если документ его упоминает (с учётом путаницы символов при
            OCR), а извлечён другой номер, номер заменяется номером экспертизы: «другой» номер в таком документе —
            обычно номер сметы, акта или счёта.

    Returns:
        tuple: (проверенная копия ``raw_data``, список удалённого — для журнала).
    """
    if not isinstance(raw, dict) or not text:
        return raw, []
    idx = _TextIndex(text)
    out = copy.deepcopy(raw)
    dropped: List[str] = []

    def keep(items: Any, check, label) -> Any:
        """Фильтр списка словарей по проверке ``check(item)``."""
        if not isinstance(items, list):
            return items
        kept = []
        for it in items:
            if isinstance(it, dict) and not check(it):
                dropped.append(f"{label}: {it.get('context') or ''} {it.get('value')}".strip())
                continue
            kept.append(it)
        return kept

    if "finances" in out:
        out["finances"] = keep(out["finances"], lambda it: amount_in_text(it.get("value"), idx.numbers), "сумма")
    if "dates" in out:
        out["dates"] = keep(out["dates"], lambda it: date_in_text(it.get("value"), idx.dates), "дата")

    cn = out.get("contract_number")
    if isinstance(cn, dict) and cn.get("value"):
        if not plausible_contract_number(cn["value"]) or not idx.has_contract_number(cn["value"]):
            dropped.append(f"номер договора: {cn['value']}")
            out.pop("contract_number")
    cn = out.get("contract_number")
    if expected_number and idx.has_contract_number(expected_number) and normalize_number(expected_number):
        current = cn.get("value") if isinstance(cn, dict) else None
        if current and normalize_number(current) != normalize_number(expected_number):
            dropped.append(f"номер договора: {current} → {expected_number} (документ упоминает договор экспертизы)")
            cn["value"] = expected_number

    for org in out.get("organizations") or []:
        if not isinstance(org, dict):
            continue
        for key in ("inn", "kpp", "ogrn"):
            if org.get(key) and not idx.has_number_id(org[key]):
                dropped.append(f"{key.upper()} {org.get('name') or ''}: {org[key]}".strip())
                org.pop(key)

    nm = out.get("nmck_method")
    if isinstance(nm, dict) and nm.get("method"):
        method = str(nm["method"]).lower()
        stems = next((v for k, v in NMCK_METHOD_STEMS.items() if k in method), None)
        mentions_nmck = "нмцк" in idx.low or re.search(r"начальн\w*\s*\(?\s*максимальн", idx.low)
        if not mentions_nmck or (stems and not any(s in idx.low for s in stems)):
            dropped.append(f"метод НМЦК: {nm['method']}")
            out.pop("nmck_method")

    pt = out.get("planned_timeline")
    if isinstance(pt, dict):
        for key in ("start_date", "end_date"):
            if pt.get(key) and not date_in_text(pt[key], idx.dates):
                dropped.append(f"срок {key}: {pt[key]}")
                pt.pop(key)

    for rep in out.get("reporting_documentation") or []:
        if isinstance(rep, dict) and rep.get("deadline") and not date_in_text(rep["deadline"], idx.dates):
            dropped.append(f"срок отчётного документа: {rep['deadline']}")
            rep.pop("deadline")

    return out, dropped


def verify_extraction(extracted: Any, text: str, filename: str = "", expected_number: Optional[str] = None) -> Any:
    """Проверяет результат ``TypeDataExtractor`` по тексту документа (см. :func:`verify_raw_data`).

    Args:
        extracted: Результат извлечения (``dict`` с ``raw_data``); другие значения возвращаются как есть.
        text: Полный текст документа.
        filename: Имя файла — для журнала.
        expected_number: Номер договора экспертизы (см. :func:`verify_raw_data`).

    Returns:
        Копия результата с проверенным ``raw_data``.
    """
    if not isinstance(extracted, dict) or not isinstance(extracted.get("raw_data"), dict):
        return extracted
    raw, dropped = verify_raw_data(extracted["raw_data"], text, expected_number)
    if not dropped:
        return extracted
    logger.warning(f"Извлечённые данные {filename}: нет в тексте документа, отброшено {len(dropped)}: {dropped[:20]}")
    result = dict(extracted)
    result["raw_data"] = raw
    return result

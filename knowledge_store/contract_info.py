"""Реквизиты контракта (номер, цена, дата) из контракта или проекта контракта экспертизы — без запросов к модели.

Источники (сохраняются при ``/evaluate-documents``, хранилище знаний):

1. Текст документа (``ai_documents.text`` / ``pe_documents.text_full``) — разбирается регулярными выражениями:
   «Контракт № 0373200001226000123», «от «12» марта 2026 г.», «Цена Контракта составляет 1 234 567,89 рублей».
2. Результат ``TypeDataExtractor`` (``pe_documents.extraction.raw_data``), уже полученный при прогоне:
   ``contract_number``, ``finances`` с контекстом «Цена контракта», ``dates`` с контекстом «Дата контракта».

Документы по приоритету: заключённый контракт (поставка, работы/услуги, НИР), затем проект контракта; более
свежий — раньше. Каждое поле берётся из первого источника, где оно найдено. Незаполненные поля проекта
(«№ ____», ««__» ______ 20__ г.») значением не считаются; НМЦК ценой контракта не считается.
"""
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

EMPTY = {"contract_number": "0", "amount": "0", "date": "0"}

# заключённые контракты раньше проекта; дополнительные соглашения не используются (меняют, а не задают реквизиты)
SIGNED_TYPES = ("docContractPostTovarFiles", "docContractDoWorkFiles", "docContractNIRFiles")
DRAFT_TYPES = ("docProjContractFiles",)
CONTRACT_TYPES = SIGNED_TYPES + DRAFT_TYPES
NAME_RE = re.compile(r"контракт|договор", re.I)
NOT_CONTRACT_NAME_RE = re.compile(r"доп\w*\.?\s*соглаш|дс\b|соглашени|расторж|акт\b|счет|сч[её]т|смет|извещ", re.I)

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}

NUMBER_RE = re.compile(
    r"(?:контракт|договор)\w*(?:\s+\w+){0,4}?\s*(?:№|N\s|No\.?|номер)\s*([0-9A-Za-zА-Яа-я][0-9A-Za-zА-Яа-я\-/.]*[0-9A-Za-zА-Яа-я])",
    re.I)
DATE_WORDS_RE = re.compile(r"[«\"']?\s*(\d{1,2})\s*[»\"']?\s+([а-яё]+)\s+(\d{4})", re.I)
DATE_DIGITS_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
HEAD_DATE_RE = re.compile(r"([«\"']?\s*\d{1,2}\s*[»\"']?\s+[а-яё]+\s+\d{4}|\d{1,2}\.\d{1,2}\.\d{4})\s*г", re.I)
PRICE_RE = re.compile(
    r"(?:цена|стоимость|сумма)\s+(?:настоящего\s+|по\s+настоящему\s+)?(?:контракта|договора)"
    r"(?![^.]{0,40}(?:начальн|максимальн))"
    r"[^0-9]{0,160}?(\d{1,3}(?:[  ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:\(|руб|₽|р\.)",
    re.I)
NMCK_RE = re.compile(r"нмц|начальн\w*\s*\(?\s*максимальн", re.I)
PRICE_CONTEXT_RE = re.compile(r"цен\w*\s+(контракт|договор)|стоимост\w*\s+(контракт|договор|работ|услуг|товар|поставк)"
                              r"|сумм\w*\s+(контракт|договор)", re.I)
DATE_CONTEXT_RE = re.compile(r"(дат\w*\s+(контракт|договор|заключ|подписан))|заключ", re.I)


def _blank(value: Any) -> bool:
    """Пустое или незаполненное значение проекта («____», «0», «№»)."""
    s = str(value or "").strip()
    return not s or s in ("0", "None", "null", "№") or bool(re.fullmatch(r"[_\s.№\-–—«»\"']*", s))


def parse_date(text: str) -> Optional[str]:
    """Дата из строки («12 марта 2026», «12.03.2026», «2026-03-12») в виде ``ГГГГ-ММ-ДД``.

    Args:
        text: Строка с датой.

    Returns:
        Optional[str]: Дата или ``None`` (не найдена или недопустима).
    """
    s = str(text or "")
    for rx, order in ((DATE_ISO_RE, "ymd"), (DATE_DIGITS_RE, "dmy"), (DATE_WORDS_RE, "dMy")):
        m = rx.search(s)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if order == "ymd":
                y, mo, d = int(a), int(b), int(c)
            elif order == "dmy":
                d, mo, y = int(a), int(b), int(c)
            else:
                word = b.lower().replace("ё", "е")
                mo = next((n for k, n in MONTHS.items() if word.startswith(k)), None)
                if mo is None or (mo == 5 and not word.startswith(("мая", "май"))):
                    continue
                d, y = int(a), int(c)
            if not 2000 <= y <= 2100:
                continue
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def parse_amount(text: Any) -> Optional[str]:
    """Сумма «1 234 567,89» / 1234567.89 → ``"1234567.89"`` (целая — без дробной части).

    Args:
        text: Строка или число.

    Returns:
        Optional[str]: Сумма или ``None``.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        value = float(text)
    else:
        s = re.sub(r"[  ]", "", str(text)).replace(",", ".")
        m = re.search(r"\d+(?:\.\d{1,2})?", s)
        if not m:
            return None
        value = float(m.group(0))
    if value <= 0:
        return None
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def from_text(text: str) -> Dict[str, Optional[str]]:
    """Номер, цена и дата контракта по тексту документа.

    Номер и дата ищутся в начале документа (заголовок «Контракт № … от …»), цена — по всему тексту
    («Цена контракта составляет …»), без НМЦК.

    Args:
        text: Текст контракта или проекта.

    Returns:
        Dict[str, Optional[str]]: ``contract_number``, ``amount``, ``date`` (``None`` — не найдено).
    """
    text = text or ""
    head = text[:4000]
    out: Dict[str, Optional[str]] = {"contract_number": None, "amount": None, "date": None}
    m = NUMBER_RE.search(head)
    if m and not _blank(m.group(1)) and re.search(r"\d", m.group(1)):
        out["contract_number"] = m.group(1).rstrip(".")
        # дата контракта — сразу за номером: «№ 123 от «12» марта 2026 г.»
        after = head[m.end():m.end() + 80]
        dm = re.match(r"\s*от\s*(.{0,40})", after, re.I)
        if dm:
            out["date"] = parse_date(dm.group(1))
    if out["date"] is None:
        # дата в шапке без номера: ««12» марта 2026 г.» / «12.03.2026 г.» (с «г.» — чтобы не взять ГОСТ или пункт)
        for dm in HEAD_DATE_RE.finditer(head[:1500]):
            out["date"] = parse_date(dm.group(1))
            if out["date"]:
                break
    for pm in PRICE_RE.finditer(text):
        before = text[max(0, pm.start() - 60):pm.start()]
        if NMCK_RE.search(before) or NMCK_RE.search(pm.group(0)):
            continue
        out["amount"] = parse_amount(pm.group(1))
        if out["amount"]:
            break
    return out


def _value(field: Any) -> Any:
    """Значение поля экстрактора (``{"value": …}`` или само значение)."""
    return field.get("value") if isinstance(field, dict) else field


def from_extraction(extraction: Any) -> Dict[str, Optional[str]]:
    """Номер, цена и дата контракта из сохранённого результата ``TypeDataExtractor``.

    Args:
        extraction: ``pe_documents.extraction`` (``raw_data`` — словарь или список словарей).

    Returns:
        Dict[str, Optional[str]]: ``contract_number``, ``amount``, ``date``.
    """
    out: Dict[str, Optional[str]] = {"contract_number": None, "amount": None, "date": None}
    raw = (extraction or {}).get("raw_data") if isinstance(extraction, dict) else None
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    for r in items:
        if not isinstance(r, dict):
            continue
        number = _value(r.get("contract_number"))
        if out["contract_number"] is None and not _blank(number) and re.search(r"\d", str(number)):
            out["contract_number"] = str(number).strip().lstrip("№").strip()
        for f in r.get("finances") or []:
            ctx = str((f or {}).get("context") or "")
            if out["amount"] is None and PRICE_CONTEXT_RE.search(ctx) and not NMCK_RE.search(ctx):
                out["amount"] = parse_amount(_value(f))
        for d in r.get("dates") or []:
            ctx = str((d or {}).get("context") or "")
            if out["date"] is None and DATE_CONTEXT_RE.search(ctx):
                out["date"] = parse_date(str(_value(d) or ""))
    return out


def rank(doc: Dict[str, Any]) -> int:
    """Приоритет документа: 0 — заключённый контракт, 1 — проект, 2 — файл с «контракт/договор» в имени."""
    kind = doc.get("detected_type") or doc.get("doc_code")
    if kind in SIGNED_TYPES:
        return 0
    if kind in DRAFT_TYPES:
        return 1
    return 2


def is_contract(doc: Dict[str, Any]) -> bool:
    """Документ — контракт или проект контракта (по типу или по имени файла)."""
    kind = doc.get("detected_type") or doc.get("doc_code")
    if kind in CONTRACT_TYPES:
        return True
    name = doc.get("filename") or ""
    return bool(NAME_RE.search(name)) and not NOT_CONTRACT_NAME_RE.search(name)


def collect(docs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Реквизиты контракта по документам экспертизы.

    Args:
        docs: ``detected_type``, ``doc_code``, ``filename``, ``text``, ``extraction``, ``created_at``
            (порядок — от свежих к старым).

    Returns:
        Dict[str, Any]: ``contract_number``, ``amount``, ``date`` (``"0"`` — не найдено) и ``sources``
        (откуда взято каждое поле: имя файла и способ ``text`` / ``extraction``).
    """
    ordered = sorted((d for d in docs if is_contract(d)), key=rank)   # sorted устойчив: свежие остаются раньше
    out: Dict[str, Any] = dict(EMPTY)
    sources: Dict[str, str] = {}
    for d in ordered:
        for how, found in (("text", from_text(d.get("text") or "")), ("extraction", from_extraction(d.get("extraction")))):
            for k, v in found.items():
                if v and k not in sources:
                    out[k], sources[k] = v, f"{d.get('filename')}: {how}"
        if len(sources) == 3:
            break
    out["sources"] = sources
    return out


SQL_DOCS = """
SELECT p.detected_type, p.doc_code, p.filename, p.extraction, p.created_at,
       LEFT(COALESCE(a.text, p.text_full), 300000) AS text
FROM pe_documents p
LEFT JOIN ai_documents a ON a.id = p.ai_document_id
WHERE p.expertise_id = $1 AND ($2::text IS NULL OR p.env = $2)
ORDER BY p.created_at DESC NULLS LAST, p.id DESC
"""


async def fetch_documents(conn: Any, expertise_id: int, env: Optional[str]) -> List[Dict[str, Any]]:
    """Документы экспертизы из хранилища знаний (без окружения — во всех окружениях, свежие раньше).

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        env: Окружение или ``None``.

    Returns:
        List[Dict[str, Any]]: Строки ``pe_documents`` с текстом.
    """
    import json

    rows = []
    for r in await conn.fetch(SQL_DOCS, int(expertise_id), env):
        d = dict(r)
        if isinstance(d.get("extraction"), (str, bytes)):
            try:
                d["extraction"] = json.loads(d["extraction"])
            except ValueError:
                d["extraction"] = None
        rows.append(d)
    return rows

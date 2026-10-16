"""Паспорт печатной формы ЕИС без модели.

Печатная форма повторяет свой XML: номера, даты, суммы, обеспечение, объект закупки разбираются
по меткам (``knowledge_store.eis_printform.PrintForm``), а не моделью. Модель нужна только для
печатной формы извещения и только для одного вопроса — нет ли в описании объекта закупки товарных
знаков (DOC-002, AI-001). Для этого кодом отбираются строки-кандидаты (латиница, «СлитноеНаписание»,
кавычки, ®/™, артикулы вида «HP-85A», слова «марка», «модель», «производитель», «эквивалент»)
и только они отправляются в модель; нет кандидатов — нет запроса.

Чистые функции — их покрывают тесты ``tests/test_rm_llm_load.py``.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from knowledge_store.eis_printform import LABELS, PrintForm, _norm

MAX_CANDIDATES = 40
MAX_CANDIDATE_CHARS = 6000

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
MONEY_RE = re.compile(r"(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:российский рубль|руб|₽|%)?", re.IGNORECASE)

DATE_LABELS = {
    "end_dt": "Дата и время окончания подачи заявок",
    "first_parts": "Дата рассмотрения первых частей заявок",
    "bidding": "Дата подачи предложений о цене",
    "second_parts": "Дата рассмотрения вторых частей заявок",
    "summarizing": "Дата подведения итогов определения поставщика",
    "start_date": "Дата начала исполнения контракта",
    "term": "Срок исполнения контракта",
}
MONEY_LABELS = {
    "price": "Начальная (максимальная) цена контракта",
    "price_max": "Максимальное значение цены контракта",
    "app_size": "Размер обеспечения заявки",
    "contract_size": "Размер обеспечения исполнения контракта",
    "warranty_size": "Размер обеспечения гарантийных обязательств",
    "advance": "Аванс",
}

# Кандидаты в товарный знак / модель / производителя
BRAND_HINT_RE = re.compile(
    r"[®™]|«[^»]{2,40}»|\"[^\"]{2,40}\""
    r"|\b[А-ЯЁA-Z][а-яёa-z]+[А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z]*\b"          # КлериГель, PowerEdge
    r"|\b[A-Za-z]{2,}[-\s]?\d+[A-Za-z0-9-]*\b|\b\d+[A-Za-z]{2,}\b"     # HP-85A, X200, 85A
    r"|\b[A-Za-z][a-z]{2,}\b"                                          # латинское слово
    r"|\bмарк[аиеу]\b|\bмодел\w*|\bпроизводител\w*|\bпроизводств\w*\s+[А-ЯЁA-Z]|\bэквивалент\w*|\bартикул\w*"
)
# Латинские сокращения, которые не являются товарными знаками
NOT_BRAND = {"iso", "gost", "usb", "hdmi", "led", "lcd", "ip", "ph", "din", "iec", "en", "astm", "si", "wi-fi", "wifi",
             "pdf", "html", "http", "https", "www", "ru", "gb", "mb", "tb", "ghz", "mhz", "vga", "dvi", "rj", "ssd",
             "hdd", "ram", "dna", "pcr", "gmp", "ce", "rohs", "atx", "mm", "cm", "kg", "ml", "mg", "uv", "ir"}
ORG_RE = re.compile(r"\b(ООО|АО|ПАО|ЗАО|ОАО|ФГБУ|ФГБНУ|ФГБОУ|ГБУ|МБУ|ФГУП|ИП)\b|@|https?://|www\.", re.IGNORECASE)
KTRU_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}(-\d{8})?\b")


def _date(value: Optional[str]) -> Optional[str]:
    """Первая дата ``ДД.ММ.ГГГГ`` из значения в ISO или ``None``."""
    m = DATE_RE.search(value or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _money(value: Optional[str]) -> Optional[float]:
    """Первое число из значения («1026336.64 РОССИЙСКИЙ РУБЛЬ» → 1026336.64) или ``None``."""
    m = MONEY_RE.search(value or "")
    if not m:
        return None
    try:
        return float(re.sub(r"\s", "", m.group(1)).replace(",", "."))
    except ValueError:
        return None


def _title(form: PrintForm) -> Optional[str]:
    """Название документа из заголовка печатной формы («Извещение о проведении…», «Изменения извещения…»)."""
    head = form.sections.get("Заголовок", [])[:15]
    for i, line in enumerate(head):
        n = _norm(line)
        if re.match(r"^(извещени|изменени|протокол|сведения|информаци|отчет|документ)", n, re.IGNORECASE) and len(n) > 15:
            # заголовок переносится на следующие строки: «Изменения извещения о проведении» / «запроса котировок…»
            for nxt in head[i + 1:i + 4]:
                if "№" in n or re.search(r"\d{11,}", n):
                    break
                m = _norm(nxt)
                if not m or re.match(r"^(номер|дата|общая информация)", m, re.IGNORECASE):
                    break
                n = f"{n} {m}"
            return n
    return None


def brand_candidates(form: PrintForm) -> List[str]:
    """Строки раздела «Объект закупки» и наименование объекта с признаками товарного знака.

    Args:
        form: Разобранная печатная форма.

    Returns:
        List[str]: Строки-кандидаты без повторов (до ``MAX_CANDIDATES``, суммарно до ``MAX_CANDIDATE_CHARS``).
    """
    lines = list(form.sections.get("Объект закупки", []))
    name = form.value("object_name")
    if name:
        lines.insert(0, name)
    out: List[str] = []
    size = 0
    for line in lines:
        n = _norm(line)
        if len(n) < 4 or ORG_RE.search(n) or re.fullmatch(r"[\d\s.,%-]+", n):
            continue
        hits = [m.group(0) for m in BRAND_HINT_RE.finditer(KTRU_RE.sub(" ", n))]
        hits = [h for h in hits if h.strip("«»\"").lower() not in NOT_BRAND]
        if not hits or n in out:
            continue
        out.append(n)
        size += len(n)
        if len(out) >= MAX_CANDIDATES or size >= MAX_CANDIDATE_CHARS:
            break
    return out


def build(text: str, print_kind: Dict[str, Any], row: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Извлечённые данные, паспорт и кандидаты в товарные знаки по тексту печатной формы.

    Args:
        text: Построчный текст печатной формы (HTML → текст).
        print_kind: ``doc_kind.print_form_kind``.
        row: Строка ``files_meta.sql`` (номер закупки, заказчик — если в форме их нет).

    Returns:
        Tuple[Dict, Dict, List[str]]: ``extraction`` (``readability``, ``raw_data`` — формат ``DataExtractor``),
        ``profile`` (формат ``DocumentProfiler``), кандидаты для проверки товарных знаков (только извещение).
    """
    form = PrintForm(text)
    title = _title(form) or print_kind.get("type_decode")
    number = re.sub(r"\D", "", form.value("number") or "") or row.get("owner_number") or row.get("purchase_number")

    def ev(key: str, label: str) -> Dict[str, Any]:
        """Доказательство: «метка значение» как в печатной форме."""
        return {"fragment": f"{LABELS.get(key, label)} {form.value(key)}"[:300], "section": None, "page": None,
                "confidence": 1.0}

    dates = [{"value": _date(form.value(k)), "context": label, "evidence": ev(k, label)}
             for k, label in DATE_LABELS.items() if _date(form.value(k))]
    finances = [{"value": _money(form.value(k)), "currency": "RUB" if "%" not in (form.value(k) or "") else "%",
                 "context": label, "evidence": ev(k, label)}
                for k, label in MONEY_LABELS.items() if _money(form.value(k)) is not None]
    org = form.value("org") or form.value("placing")
    organizations = [{"role": "заказчик", "name": re.sub(r"^(Заказчик|Уполномоченный орган)\s+", "", org),
                      "inn": None, "kpp": None, "ogrn": None, "address": form.value("post_address"),
                      "evidence": ev("org" if form.value("org") else "placing", "Организация")}] if org else []
    subject = form.value("object_name") or row.get("purchase_subject")
    price = next((f["value"] for f in finances if f["context"].startswith(("Начальная", "Максимальное"))), None)
    end_dt = next((d["value"] for d in dates if d["context"].startswith("Дата и время окончания")), None)
    summary = ". ".join(x for x in [
        title,
        f"Объект закупки: {subject}" if subject else None,
        f"Цена: {price:,.2f} руб".replace(",", " ") if price else None,
        f"Окончание подачи заявок: {end_dt}" if end_dt else None,
        f"Срок исполнения контракта: {form.value('term')}" if form.value("term") else None,
    ] if x) + "."
    raw_data = {
        "document_name": {"value": title, "confidence": 1.0},
        "summary": summary,
        "contract_number": {"value": number, "evidence": ev("number", "Номер извещения") if form.value("number") else None},
        "dates": dates,
        "finances": finances,
        "organizations": organizations,
        "procurement_subject": {"description": subject or "", "evidence": None, "items": []},
        "execution_location": {"address": form.value("place") or "", "special_conditions": "", "evidence": None},
        "source": "print_form",
    }
    extraction = {"readability": {"status": "allow", "readability_score": 1.0, "main_language": "ru", "issues": []},
                  "raw_data": raw_data}
    key_conditions = [{"name": d["context"], "value": d["value"], "section": None} for d in dates] + \
                     [{"name": f["context"], "value": f"{f['value']:,.2f} {f['currency']}".replace(",", " "), "section": None}
                      for f in finances]
    sections = [s for s in form.sections if s != "Заголовок"]
    profile = {
        "resume": summary,
        "purpose": subject or title,
        "structure": {"main_sections": sections, "has_tables": "Объект закупки" in form.sections,
                      "has_appendices": "Перечень прикрепленных документов" in form.sections,
                      "appendices": [_norm(x) for x in form.sections.get("Перечень прикрепленных документов", [])
                                     if re.search(r"\.[A-Za-z]{2,5}$", _norm(x))][:30]},
        "key_conditions": key_conditions,
        "semantics": {"topics": [], "keywords": [], "classification": {}},
        "technical_objects": {"brands": [], "models": [], "manufacturers": [], "standards": []},
        "assessment": {},
        "recommendations": [],
        "confidence": 1.0,
        "source": "print_form",
    }
    candidates = brand_candidates(form) if print_kind.get("group") == "notice" else []
    return extraction, profile, candidates

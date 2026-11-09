"""Разбор документов со стабильным форматом кодом, без модели: платёжные поручения и XML электронных площадок.

Модель извлекала суммы платёжек нестабильно: в двух прогонах экспертизы 7185 итог по семи платёжкам отличался на
сумму одной из них (8 238 093,54), потому что в одном прогоне её сумма не была извлечена. У платёжного поручения
(форма 0401060) поля стоят на своих местах: номер после «ПЛАТЕЖНОЕ ПОРУЧЕНИЕ №», сумма после «Сумма» в виде
``2076618-96``, ИНН плательщика и получателя, назначение с номером договора. XML площадок читается как строки
«тег: значение» (``FileReader.read_xml_file``) — цена договора, НМЦК, номер, даты берутся по тегам.

Результат — частичный ``raw_data`` в формате извлечения (с ``evidence.fragment`` — строкой из текста, ``kind`` —
видом значения). Для платёжки, разобранной полностью, модель для извлечения не вызывается; XML дополняет ответ
модели (:func:`overlay`).
"""
import re
from datetime import date
from typing import Any, Dict, List, Optional

from evaluate_documents.evidence_check import inn_valid, normalize_number, plausible_contract_number

PAYMENT_TITLE_RE = re.compile(r"пл[аa]т[её]жн\w*\s+поручени\w*\s*(?:№|N|No)\s*(\d{1,10})", re.I)
AMOUNT_RE = re.compile(r"Сумма(?![\s:]*пропись)[\s:]*(\d[\d  ]{0,20}[-=.,]\d{2})(?!\d)", re.I)
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
INN_RE = re.compile(r"ИНН\s*:?\s*(\d{10}|\d{12})(?!\d)", re.I)
CONTRACT_IN_PURPOSE_RE = re.compile(
    r"(?:договор|контракт)\w*\s+(?:от\s+\d{1,2}\.\d{1,2}\.\d{2,4}\s*(?:г\.?)?\s*)?(?:№|N)\s*"
    r"([A-ZА-ЯЁ0-9][\wА-ЯЁа-яё\-/.]*[\wА-ЯЁа-яё])", re.I)
PURPOSE_RE = re.compile(r"[^\n]*(?:договор|контракт|оплат|по\s+сч\.?|аванс)[^\n]*", re.I)


def _iso(day: str, month: str, year: str) -> Optional[str]:
    """Дата ISO из частей (``None`` для невозможной даты)."""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _amount(value: str) -> Optional[float]:
    """Сумма из записи ``2076618-96`` / ``2 076 618,96``."""
    s = re.sub(r"[\s ]", "", value)
    s = re.sub(r"[-=,](\d{2})$", r".\1", s)
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _line_with(text: str, start: int, end: int) -> str:
    """Строка текста, содержащая позицию ``start``…``end`` (для ``evidence.fragment``)."""
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    return text[left: right if right != -1 else len(text)].strip()[:300]


def _evidence(fragment: str) -> Dict[str, Any]:
    """Доказательство для значения, найденного кодом."""
    return {"fragment": fragment, "section": None, "page": None, "confidence": 1.0}


def _name_after(text: str, pos: int) -> Optional[str]:
    """Наименование организации — следующая непустая строка после строки с ИНН (без хвоста «Сч. № …»)."""
    rest = text[text.find("\n", pos) + 1:] if text.find("\n", pos) != -1 else ""
    for line in rest.splitlines()[:3]:
        line = re.split(r"\s{3,}|Сч\.\s*№", line.strip())[0].strip()
        if len(line) >= 3 and re.search(r"[А-ЯЁA-Z]{2}", line) and not re.match(r"^(Плательщик|Получатель|Банк)", line):
            return line[:200]
    return None


def parse_payment_order(text: str, filename: str = "") -> Optional[Dict[str, Any]]:
    """Разбирает платёжное поручение.

    Args:
        text: Текст документа (текстовый слой или OCR).
        filename: Имя файла (для заголовка, если номер не найден).

    Returns:
        dict | None: ``{"complete": bool, "raw_data": {...}}`` или ``None``, если это не платёжное поручение.
        ``complete`` — найдены номер, дата и сумма: извлечение моделью не требуется.
    """
    if not text:
        return None
    title = PAYMENT_TITLE_RE.search(text)
    if not title:
        return None
    raw: Dict[str, Any] = {"dates": [], "finances": [], "organizations": [], "other_data": []}
    number = title.group(1)
    raw["document_name"] = {"value": f"Платёжное поручение № {number}", "confidence": 1.0}

    pay_date = None
    m = DATE_RE.search(text, title.end(), title.end() + 120)
    if m:
        pay_date = _iso(*m.groups())
        if pay_date:
            raw["dates"].append({"evidence": _evidence(_line_with(text, title.start(), m.end())), "kind": "payment",
                                 "value": pay_date, "context": "Дата платёжного поручения"})

    amount = None
    m = AMOUNT_RE.search(text)
    if m:
        amount = _amount(m.group(1))
        if amount:
            raw["finances"].append({"evidence": _evidence(_line_with(text, m.start(), m.end())), "kind": "payment",
                                    "value": amount, "currency": "RUB", "context": "Сумма платежа"})

    inns = [(mm.group(1), mm.start()) for mm in INN_RE.finditer(text) if inn_valid(mm.group(1))]
    seen = []
    for inn, pos in inns:
        if inn not in [s for s, _ in seen]:
            seen.append((inn, pos))
    for (inn, pos), role in zip(seen[:2], ("плательщик", "получатель")):
        raw["organizations"].append({"evidence": _evidence(_line_with(text, pos, pos + 15)), "role": role,
                                     "name": _name_after(text, pos), "inn": inn})

    purpose = None
    for pm in PURPOSE_RE.finditer(text):
        line = pm.group(0).strip()
        if CONTRACT_IN_PURPOSE_RE.search(line) or re.search(r"оплат|аванс", line, re.I):
            purpose = line[:300]
            break
    if purpose:
        raw["other_data"].append({"evidence": _evidence(purpose), "value": f"Назначение платежа: {purpose}"})
        cm = CONTRACT_IN_PURPOSE_RE.search(purpose)
        if cm and plausible_contract_number(cm.group(1)):
            raw["contract_number"] = {"evidence": _evidence(purpose), "value": cm.group(1).rstrip(".")}

    money = f" на сумму {amount:,.2f} руб.".replace(",", " ").replace(".", ",", 1) if amount else ""
    when = f" от {pay_date}" if pay_date else ""
    contract = f", по договору № {raw['contract_number']['value']}" if raw.get("contract_number") else ""
    payer = next((o for o in raw["organizations"] if o["role"] == "плательщик"), None)
    payee = next((o for o in raw["organizations"] if o["role"] == "получатель"), None)
    parties = ""
    if payer and payee:
        parties = f"; плательщик ИНН {payer['inn']}, получатель ИНН {payee['inn']}"
    raw["summary"] = f"Платёжное поручение № {number}{when}{money}{contract}{parties}."
    return {"complete": bool(number and pay_date and amount), "raw_data": raw}


# ---------------- XML площадок («тег: значение») ----------------

XML_LINE_RE = re.compile(r"^([A-Za-z_][\w.-]{0,60}):\s?(.*)$")
XML_NUMBER_TAGS = ("customer_code", "contract_number", "contractnumber", "number_contract", "contract_num")
XML_AMOUNT_TAGS = {
    "amount": "contract_price", "contract_price": "contract_price", "contractprice": "contract_price",
    "price_contract": "contract_price",
    "max_price": "nmck", "maxprice": "nmck", "nmck": "nmck", "initial_sum": "nmck", "initialsum": "nmck",
    "start_price": "nmck", "startprice": "nmck",
}
# sign_date в XML площадок — дата электронной подписи (участника, протокола), а не заключения договора
XML_DATE_TAGS = {"sign_date": "other", "signdate": "other", "contract_date": "signing",
                 "publish_date": "publication", "publishdate": "publication", "protocol_date": "protocol",
                 "end_date": "work_end", "enddate": "work_end", "execution_end": "work_end"}
XML_REG_TAGS = ("external_code", "purchase_number", "purchasenumber", "reg_number", "regnumber", "notice_number")


def looks_like_flat_xml(text: str) -> bool:
    """Текст — XML, прочитанный как строки «тег: значение» (не меньше 60 % строк, не меньше 5 строк)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    return sum(1 for ln in lines if XML_LINE_RE.match(ln.strip())) / len(lines) >= 0.6


def parse_flat_xml(text: str) -> Optional[Dict[str, Any]]:
    """Номер договора, цена, НМЦК, ценовое предложение, даты и реестровый номер закупки из XML площадки.

    Args:
        text: Текст XML в виде строк «тег: значение».

    Returns:
        dict | None: Частичный ``raw_data``; ``None`` — не XML или ничего не найдено.
    """
    if not looks_like_flat_xml(text):
        return None
    pairs = []
    for ln in text.splitlines():
        m = XML_LINE_RE.match(ln.strip())
        if m and m.group(2).strip():
            pairs.append((m.group(1).lower(), m.group(2).strip(), ln.strip()))
    tags = {t for t, _, _ in pairs}
    raw: Dict[str, Any] = {"dates": [], "finances": [], "other_data": []}
    for tag, value, line in pairs:
        if tag in XML_NUMBER_TAGS and "contract_number" not in raw and plausible_contract_number(value):
            raw["contract_number"] = {"evidence": _evidence(line), "value": value}
        elif tag in XML_AMOUNT_TAGS or (tag == "price" and tags & {"max_price", "maxprice", "start_price"}):
            kind = XML_AMOUNT_TAGS.get(tag, "offer_price")
            amount = _amount(value) if re.fullmatch(r"\d+(?:[.,]\d{1,2})?", value) else None
            if amount and amount > 0:
                context = {"contract_price": "Цена договора", "nmck": "Начальная (максимальная) цена",
                           "offer_price": "Ценовое предложение участника"}[kind]
                raw["finances"].append({"evidence": _evidence(line), "kind": kind, "value": amount,
                                        "currency": "RUB", "context": f"{context} (XML: {tag})"})
        elif tag in XML_DATE_TAGS:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
            if m:
                iso = _iso(m.group(3), m.group(2), m.group(1))
                if iso:
                    raw["dates"].append({"evidence": _evidence(line), "kind": XML_DATE_TAGS[tag], "value": iso,
                                         "context": f"XML: {tag}"})
        elif tag in XML_REG_TAGS and re.fullmatch(r"\d{11,19}", value):
            raw["other_data"].append({"evidence": _evidence(line), "value": f"Реестровый номер закупки в ЕИС: {value}"})
    if not (raw.get("contract_number") or raw["finances"] or raw["dates"] or raw["other_data"]):
        return None
    return raw


# ---------------- наложение на ответ модели ----------------

def overlay(raw: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Дополняет ``raw_data`` значениями, найденными кодом (они точнее модели).

    * сумма/дата из ``parsed`` с тем же значением, что у модели, — у записи модели проставляется ``kind`` (если
      не было), иначе запись добавляется;
    * номер договора и название документа — из ``parsed``, если у модели пусто;
    * ``other_data`` и стороны — добавляются, если такого значения/ИНН ещё нет.

    Args:
        raw: ``raw_data`` после объединения блоков (меняется на месте).
        parsed: Частичный ``raw_data`` из :func:`parse_payment_order` / :func:`parse_flat_xml`.

    Returns:
        dict: Тот же ``raw``.
    """
    for key, same in (("finances", lambda a, b: _num(a.get("value")) == _num(b.get("value"))),
                      ("dates", lambda a, b: str(a.get("value"))[:10] == str(b.get("value"))[:10])):
        items = raw.setdefault(key, [])
        for new in parsed.get(key) or []:
            match = next((it for it in items if isinstance(it, dict) and same(it, new)), None)
            if match is None:
                items.append(new)
            elif not match.get("kind"):
                match["kind"] = new.get("kind")
    cn = raw.get("contract_number")
    if parsed.get("contract_number") and not (isinstance(cn, dict) and cn.get("value")):
        raw["contract_number"] = parsed["contract_number"]
    dn = raw.get("document_name")
    if parsed.get("document_name") and not (isinstance(dn, dict) and dn.get("value")):
        raw["document_name"] = parsed["document_name"]
    others = raw.setdefault("other_data", [])
    for new in parsed.get("other_data") or []:
        if not any(isinstance(o, dict) and o.get("value") == new.get("value") for o in others):
            others.append(new)
    orgs = raw.setdefault("organizations", [])
    known = {re.sub(r"\D", "", str(o.get("inn") or "")) for o in orgs if isinstance(o, dict)}
    for new in parsed.get("organizations") or []:
        if new.get("inn") not in known and new.get("name"):
            orgs.append(new)
    return raw


def _num(value: Any) -> Optional[float]:
    """Число для сравнения сумм."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def same_contract(a: Any, b: Any) -> bool:
    """Номера договоров совпадают с учётом путаницы символов при OCR."""
    return bool(a) and bool(b) and normalize_number(a) == normalize_number(b)

"""Извлечение данных кодом для документов со стабильным форматом (19.11.2026): XML-файлы и платёжные поручения.

Замеры прогона за 17.11.2026 (``stages.files.timings``): извлечение данных моделью — 56 % времени файлов. Самые долгие
файлы — XML: «Электронный контракт.xml» (511 с, 67 запросов), «Контракт с учетом доп. соглашений 1, 2, 3.xml»
(274 с). XML читается как строки «тег: значение» (``FileReader.read_xml_file``), и модель разбирала их блоками по
10 тыс. токенов, хотя значения стоят под своими тегами.

* **XML** (:func:`is_flat_xml`): реквизиты — кодом по тегам (:func:`parse_xml`: номер, реестровый номер, цены,
  даты, срок исполнения, предмет, стороны по ИНН); модели отдаётся только начало документа
  (``RM_XML_MODEL_CHUNKS`` блоков, по умолчанию 1) — для названия, краткого описания и того, чего нет в тегах;
  значения кода накладываются поверх ответа модели (:func:`evaluate_documents.rule_extractors.overlay`).
* **Платёжное поручение** (форма 0401060): разбор :func:`rule_extractors.parse_payment_order`; разобранное полностью
  (номер, дата, сумма) — без модели, как в ``/evaluate-documents``.

Формат результата — как у ``DataExtractor``: ``{"readability": …, "raw_data": …}``; ``source`` говорит, откуда данные.
"""
import re
from typing import Any, Dict, List, Optional

from evaluate_documents import rule_extractors as rx

# Теги XML ЕИС (контракт, сведения о контракте) и площадок, которых нет в rule_extractors (там — теги площадок):
# сравниваются в нижнем регистре
REG_TAGS = ("regnum", "regnumber", "reg_number", "contractregnum")
NUMBER_TAGS = ("number", "contractnumber", "contract_number", "docnumber")
PRICE_TAGS = ("price", "pricerur", "contractprice", "contract_price", "amount")
NMCK_TAGS = ("maxprice", "max_price", "nmck", "initialsum", "startprice")
DATE_TAGS = {"signdate": "signing", "contractdate": "signing", "conclusiondate": "signing",
             "startdate": "contract_start", "enddate": "contract_end", "executiondate": "contract_end",
             "publishdate": "publication", "protocoldate": "protocol"}
SUBJECT_TAGS = ("contractsubject", "subject", "purchaseobjectinfo", "purchase_object_info")
NAME_TAGS = ("fullname", "shortname", "firmname", "organizationname", "orgname", "name")
NAME_WINDOW = 6   # строк до и после строки с ИНН, где ищется наименование организации
MAX_ORGS = 20


def is_flat_xml(file_name: str, text: str) -> bool:
    """Файл — XML, прочитанный как строки «тег: значение» (расширение ``.xml`` и формат текста)."""
    return str(file_name or "").lower().endswith(".xml") and rx.looks_like_flat_xml(text or "")


def _ev(line: str) -> Dict[str, Any]:
    """Доказательство — строка «тег: значение» из текста."""
    return {"fragment": line[:300], "section": None, "page": None, "confidence": 1.0}


def _pairs(text: str) -> List[tuple]:
    """Строки XML: ``(тег в нижнем регистре, значение, строка)`` по порядку; пустые значения пропускаются."""
    out = []
    for ln in (text or "").splitlines():
        m = rx.XML_LINE_RE.match(ln.strip())
        if m and m.group(2).strip():
            out.append((m.group(1).lower(), m.group(2).strip(), ln.strip()))
    return out


def _amount(value: str) -> Optional[float]:
    """Сумма из значения тега (``123456.78``); иное — ``None``."""
    if not re.fullmatch(r"\d{1,15}(?:[.,]\d{1,2})?", value or ""):
        return None
    v = float(value.replace(",", "."))
    return round(v, 2) if v > 0 else None


def parse_xml(text: str) -> Optional[Dict[str, Any]]:
    """Реквизиты XML кодом: частичный ``raw_data`` (как у модели, с ``evidence.fragment`` — строкой текста).

    Берётся первое вхождение каждого реквизита: в XML ЕИС и площадок шапка документа (номер, цена, даты, стороны)
    идёт раньше вложенных разделов (этапы, позиции, приложения), где те же теги повторяются в другом смысле. Номер
    по общему тегу ``number`` и цена по ``price`` — только если в документе есть реестровый номер (XML ЕИС), иначе
    ``number`` бывает номером чего угодно. Сверх этого — разбор тегов площадок :func:`rule_extractors.parse_flat_xml`.

    Args:
        text: Текст XML в виде строк «тег: значение».

    Returns:
        Optional[Dict[str, Any]]: Частичный ``raw_data`` или ``None`` (не XML / ничего не найдено).
    """
    if not rx.looks_like_flat_xml(text or ""):
        return None
    pairs = _pairs(text)
    tags = {t for t, _, _ in pairs}
    eis = bool(tags & set(REG_TAGS))
    raw: Dict[str, Any] = {"dates": [], "finances": [], "organizations": [], "other_data": []}
    seen_dates = set()
    for tag, value, line in pairs:
        if tag in REG_TAGS and re.fullmatch(r"\d{11,25}", value) and not any(
                "Реестровый номер" in str(o.get("value")) for o in raw["other_data"]):
            raw["other_data"].append({"evidence": _ev(line), "value": f"Реестровый номер: {value}"})
        elif tag in NUMBER_TAGS and "contract_number" not in raw and (eis or tag != "number") \
                and rx.plausible_contract_number(value):
            raw["contract_number"] = {"evidence": _ev(line), "value": value}
        elif tag in PRICE_TAGS and (eis or tag != "price") and not any(
                f.get("kind") == "contract_price" for f in raw["finances"]):
            amount = _amount(value)
            if amount:
                raw["finances"].append({"evidence": _ev(line), "kind": "contract_price", "value": amount,
                                        "currency": "RUB", "context": f"Цена контракта (XML: {tag})"})
        elif tag in NMCK_TAGS and not any(f.get("kind") == "nmck" for f in raw["finances"]):
            amount = _amount(value)
            if amount:
                raw["finances"].append({"evidence": _ev(line), "kind": "nmck", "value": amount, "currency": "RUB",
                                        "context": f"Начальная (максимальная) цена (XML: {tag})"})
        elif tag in DATE_TAGS and (eis or tag != "signdate") and DATE_TAGS[tag] not in seen_dates:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
            iso = rx._iso(m.group(3), m.group(2), m.group(1)) if m else None
            if iso:
                seen_dates.add(DATE_TAGS[tag])
                raw["dates"].append({"evidence": _ev(line), "kind": DATE_TAGS[tag], "value": iso,
                                     "context": f"XML: {tag}"})
        elif tag in SUBJECT_TAGS and "procurement_subject" not in raw and len(value) >= 10:
            raw["procurement_subject"] = {"evidence": _ev(line), "description": value[:1000]}
    raw["organizations"] = _organizations(pairs)
    starts = {d["kind"]: d for d in raw["dates"]}
    if starts.get("contract_start") or starts.get("contract_end"):
        raw["planned_timeline"] = {"evidence": (starts.get("contract_start") or starts["contract_end"])["evidence"],
                                   "start_date": (starts.get("contract_start") or {}).get("value"),
                                   "end_date": (starts.get("contract_end") or {}).get("value")}
    platform = rx.parse_flat_xml(text) or {}
    if platform:
        rx.overlay(raw, platform)
    found = (raw.get("contract_number") or raw["finances"] or raw["dates"] or raw["organizations"]
             or raw["other_data"] or raw.get("procurement_subject"))
    return raw if found else None


def _organizations(pairs: List[tuple]) -> List[Dict[str, Any]]:
    """Стороны по строкам с ИНН: наименование — ближайший тег названия в пределах :data:`NAME_WINDOW` строк, КПП —
    ближайший ``kpp``; повторный ИНН не добавляется."""
    out, seen = [], set()
    for i, (tag, value, line) in enumerate(pairs):
        if tag != "inn" or not re.fullmatch(r"\d{10}|\d{12}", value) or value in seen:
            continue
        if not rx.inn_valid(value):
            continue
        seen.add(value)
        lo, hi = max(0, i - NAME_WINDOW), min(len(pairs), i + NAME_WINDOW + 1)
        near = sorted(range(lo, hi), key=lambda j: abs(j - i))
        name = next((pairs[j][1] for j in near if pairs[j][0] in NAME_TAGS and len(pairs[j][1]) >= 3), None)
        kpp = next((pairs[j][1] for j in near if pairs[j][0] == "kpp" and re.fullmatch(r"\d{9}", pairs[j][1])), None)
        out.append({"evidence": _ev(line), "role": None, "name": name, "inn": value, "kpp": kpp})
        if len(out) >= MAX_ORGS:
            break
    return out


def summary_line(raw: Dict[str, Any], file_name: str) -> str:
    """Краткое описание по реквизитам (если модель не дала своего): «XML-документ «…»: № …, цена …, стороны: …»."""
    parts = []
    cn = raw.get("contract_number")
    if isinstance(cn, dict) and cn.get("value"):
        parts.append(f"№ {cn['value']}")
    price = next((f for f in raw.get("finances") or [] if f.get("kind") == "contract_price"), None)
    if price:
        parts.append(f"цена {price['value']:,.2f} руб.".replace(",", " "))
    subj = raw.get("procurement_subject")
    if isinstance(subj, dict) and subj.get("description"):
        parts.append(f"предмет: {subj['description'][:200]}")
    orgs = [o.get("name") or f"ИНН {o.get('inn')}" for o in raw.get("organizations") or []][:3]
    if orgs:
        parts.append("стороны: " + "; ".join(orgs))
    return f"XML-документ «{file_name}»" + (": " + ", ".join(parts) if parts else "") + "."


def readability_ok() -> Dict[str, Any]:
    """Оценка читаемости для разобранного кодом документа (текст машинный, OCR не было)."""
    return {"status": "allow", "issues": [], "readability_score": 1.0, "main_language": "ru",
            "language_confidence": 1.0}


def payment_extraction(text: str, file_name: str) -> Optional[Dict[str, Any]]:
    """Платёжное поручение, разобранное кодом полностью (номер, дата, сумма), — результат без модели; иначе ``None``."""
    parsed = rx.parse_payment_order(text or "", file_name or "")
    if not parsed or not parsed.get("complete"):
        return None
    return {"readability": readability_ok(), "raw_data": parsed["raw_data"], "source": "payment_code"}


def xml_extraction(model_result: Optional[Dict[str, Any]], parsed: Optional[Dict[str, Any]],
                   file_name: str) -> Dict[str, Any]:
    """Итог извлечения XML: ответ модели по началу документа (может не быть) + реквизиты кода поверх.

    Args:
        model_result: Ответ ``DataExtractor`` по первым блокам или ``None`` (``RM_XML_MODEL_CHUNKS = 0`` / сбой).
        parsed: :func:`parse_xml`.
        file_name: Имя файла (для краткого описания).

    Returns:
        Dict[str, Any]: ``readability``, ``raw_data``, ``source = xml_code`` (+ ``model_chunks``).
    """
    base = dict(model_result or {})
    raw = dict(base.get("raw_data") or {})
    if parsed:
        rx.overlay(raw, parsed)
        for key in ("procurement_subject", "planned_timeline"):
            if parsed.get(key) and not raw.get(key):
                raw[key] = parsed[key]
    if not raw.get("document_name"):
        raw["document_name"] = {"value": file_name, "confidence": 0.5}
    if not raw.get("summary"):
        raw["summary"] = summary_line(raw, file_name)
    return {"readability": base.get("readability") or readability_ok(), "raw_data": raw, "source": "xml_code"}

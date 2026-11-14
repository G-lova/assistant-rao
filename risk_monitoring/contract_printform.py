"""Паспорт печатной формы контракта ЕИС по XML контракта (``contract``, 44-ФЗ, схема 16.x).

Печатная форма электронного контракта («Печатная форма электронного контракта.html») — вложение XML контракта
(``scanDocuments/CPEPAttachment``) и построена из тех же данных. Разбор её текста правилами извещения
(:mod:`risk_monitoring.printform_passport`) почти ничего не находит: метки у контракта другие, а после
«Наименование объекта закупки» в значение уходил весь текст контракта (файл 29759, 13.11.2026, HTTP 413).

Поэтому, если XML, к которому приложен файл, прочитан (поиск прошлых редакций читает его и кладёт в кэш прогона),
паспорт берётся из XML: номер и реестровый номер, даты, цена, аванс, обеспечение, стороны, место поставки,
позиции. Формат результата — как у ``printform_passport.build`` (``extraction`` в формате ``DataExtractor`` и
``profile`` в формате ``DocumentProfiler``), ``raw_data.source = "print_form_xml"``. Модель не используется.

Доказательство каждого значения — «подпись значение» с пометкой раздела ``XML ЕИС``: цитаты из HTML нет, но
значение совпадает с печатной формой, так как она построена из этого XML.
"""
from typing import Any, Dict, List, Optional, Tuple

MAX_ITEMS = 50                 # позиций в raw_data.items (у крупных контрактов — сотни позиций с характеристиками)
NAME_MAX_CHARS = 300
SUBJECT_MAX_CHARS = 2000
SECTION = "XML ЕИС"


def _get(node: Any, *path: str) -> Any:
    """Значение по пути ключей в нормализованном XML; список на пути — берётся первый элемент."""
    for key in path:
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, list):
        node = node[0] if node else None
    return node


def _all(node: Any, *path: str) -> List[Any]:
    """Все элементы по пути: последний ключ может быть списком (``products/product``) — он не сокращается до первого,
    как в :func:`_get`."""
    parent = _get(node, *path[:-1]) if len(path) > 1 else node
    return _list(parent.get(path[-1])) if isinstance(parent, dict) else []


def _list(node: Any) -> List[Any]:
    """Узел как список (xmltodict даёт словарь для одного элемента и список для нескольких)."""
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _num(value: Any) -> Optional[float]:
    """Число из строки XML (``1900000.00`` → ``1900000.0``); не число — ``None``."""
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> Optional[str]:
    """Дата ``ГГГГ-ММ-ДД`` из даты или даты-времени XML."""
    s = str(value or "").strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None


def _cap(value: Optional[str], limit: int) -> Optional[str]:
    """Строка не длиннее ``limit`` символов."""
    if not value or len(value) <= limit:
        return value
    return value[:limit - 1].rstrip() + "…"


def _ev(label: str, value: Any) -> Dict[str, Any]:
    """Доказательство «подпись значение» из XML."""
    return {"fragment": f"{label} {value}"[:300], "section": SECTION, "page": None, "confidence": 1.0}


def _money_str(value: float) -> str:
    """``1900000.0`` → ``1 900 000,00``."""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _supplier(info: Dict[str, Any]) -> Dict[str, Any]:
    """Поставщик из ``supplierInfo``: юрлицо, ИП или физлицо (РФ и иностранные — общие поля).

    Returns:
        Dict[str, Any]: ``name``, ``inn``, ``kpp``, ``address`` (что найдено).
    """
    for kind, node in info.items():
        if not isinstance(node, dict) or kind in ("supplierAccountsDetails",):
            continue
        egr = node.get("EGRULInfo") or node.get("EGRIPInfo") or node.get("nameInfo") or node
        name = egr.get("fullName") or egr.get("shortName")
        if not name and (egr.get("lastName") or egr.get("firstName")):
            name = " ".join(x for x in (egr.get("lastName"), egr.get("firstName"), egr.get("middleName")) if x)
            if "IP" in kind or "IndEntr" in kind or _get(node, "otherInfo", "isIP") == "true":
                name = f"ИП {name}"
        inn = egr.get("INN") or egr.get("inn") or _get(node, "taxPayerCode")
        if name or inn:
            return {"name": name, "inn": inn, "kpp": egr.get("KPP") or egr.get("kpp"),
                    "address": egr.get("address") or _get(node, "otherInfo", "postAddressInfo", "mailingAdress")}
    return {}


def _units(body: Dict[str, Any]) -> Dict[str, str]:
    """Коды ОКЕИ → обозначения из ``addInformationFromClassifiers`` (``055`` → ``М2``)."""
    out = {}
    for okei in _all(body, "addInformationFromClassifiers", "OKEI"):
        if isinstance(okei, dict) and okei.get("code"):
            out[str(okei["code"])] = okei.get("nationalName") or okei.get("fullName") or str(okei["code"])
    return out


def _items(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Позиции контракта (``products/product``), не больше ``MAX_ITEMS``; характеристики КТРУ — без значений."""
    units = _units(body)
    out = []
    for p in _all(body, "products", "product")[:MAX_ITEMS]:
        if not isinstance(p, dict):
            continue
        name = _cap(p.get("name"), NAME_MAX_CHARS)
        code = _get(p, "KTRU", "code") or _get(p, "OKPD2", "code")
        cls = "КТРУ" if _get(p, "KTRU", "code") else "ОКПД2"
        unit = units.get(str(_get(p, "OKEI", "code") or ""), _get(p, "OKEI", "code"))
        price, total = _num(p.get("price")), _num(p.get("sum"))
        specs = "; ".join(x for x in (
            f"{cls} {code}" if code else None,
            {"PRODUCT": "товар", "WORK": "работа", "SERVICE": "услуга"}.get(p.get("type"), p.get("type")),
            f"цена за ед. {_money_str(price)}" if price is not None else None,
            f"сумма {_money_str(total)}" if total is not None else None,
        ) if x)
        out.append({"name": name, "quantity": p.get("quantity"), "unit": unit, "specifications": specs,
                    "evidence": _ev(f"Позиция {p.get('indexNum') or len(out) + 1}:", name)})
    return out


def build(body: Dict[str, Any], print_kind: Dict[str, Any], row: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Извлечённые данные и паспорт печатной формы контракта по XML контракта.

    Args:
        body: Нормализованное тело XML контракта (``xml_events.parse_xml``).
        print_kind: ``doc_kind.print_form_kind``.
        row: Строка ``files_meta.sql`` (номер закупки, предмет — если в XML их нет).

    Returns:
        Tuple[Dict, Dict]: ``extraction`` (``readability``, ``raw_data``) и ``profile`` — как у
        ``printform_passport.build`` (без кандидатов в товарные знаки).
    """
    number = body.get("number")
    reg_num = body.get("regNum")
    order = _get(body, "foundation", "fcsOrder", "order") or _get(body, "foundation", "order") or {}
    purchase_number = (order.get("notificationNumber") if isinstance(order, dict) else None) or row.get("purchase_number")
    subject = _cap(body.get("contractSubject") or row.get("purchase_subject"), SUBJECT_MAX_CHARS)
    title = f"Контракт № {number}" if number else (print_kind.get("type_decode") or "Контракт")

    dates = []
    for label, kind, value in (
            ("Дата заключения контракта", "signing", body.get("signDate")),
            ("Дата начала исполнения контракта", "contract_start", _get(body, "executionPeriod", "startDate")),
            ("Дата окончания исполнения контракта", "contract_end", _get(body, "executionPeriod", "endDate")),
            ("Дата протокола определения поставщика", "protocol", body.get("protocolDate")),
            ("Дата размещения в ЕИС", "publication", body.get("publishDate") or body.get("placementDate"))):
        if _date(value):
            dates.append({"value": _date(value), "context": label, "kind": kind, "evidence": _ev(label, _date(value))})

    currency = _get(body, "priceInfo", "currency", "code") or "RUB"
    finances = []
    for label, kind, value in (
            ("Цена контракта", "contract_price", _get(body, "priceInfo", "price")),
            ("Аванс", "advance", _get(body, "advancePaymentSum", "priceValue")),
            ("Обеспечение исполнения контракта", "security",
             _get(body, "enforcement", "bankGuarantee", "guaranteeAmount") or _get(body, "enforcement", "amount")
             or _get(body, "enforcement", "cashAccount", "amount"))):
        n = _num(value)
        if n is not None:
            finances.append({"value": n, "currency": currency, "context": label, "kind": kind,
                             "evidence": _ev(label, _money_str(n))})
    advance_pct = _num(_get(body, "advancePaymentSum", "sumInPercents"))
    if advance_pct is not None:
        finances.append({"value": advance_pct, "currency": "%", "context": "Аванс, % от цены контракта",
                         "kind": "advance", "evidence": _ev("Аванс, %", f"{advance_pct:g}")})

    organizations = []
    customer = body.get("customer") or {}
    if isinstance(customer, dict) and (customer.get("fullName") or customer.get("inn")):
        organizations.append({"role": "заказчик", "name": customer.get("fullName"), "inn": customer.get("inn"),
                              "kpp": customer.get("kpp"), "ogrn": None, "address": None,
                              "evidence": _ev("Заказчик", customer.get("fullName") or customer.get("inn"))})
    for info in _all(body, "suppliersInfo", "supplierInfo"):
        sup = _supplier(info) if isinstance(info, dict) else {}
        if sup:
            organizations.append({"role": "поставщик", **{k: sup.get(k) for k in ("name", "inn", "kpp", "address")},
                                  "ogrn": None, "evidence": _ev("Поставщик", sup.get("name") or sup.get("inn"))})

    gar = _get(body, "deliveryPlaceInfo", "byGARInfo") or {}
    address = (_get(gar, "GARInfo", "GARAddress") if isinstance(gar, dict) else None) or ""
    place = (gar.get("deliveryPlace") if isinstance(gar, dict) else None) or ""
    warranty = _get(body, "qualityGuaranteeInfo", "providedPeriod", "otherPeriodText")
    items = _items(body)
    price = next((f["value"] for f in finances if f["kind"] == "contract_price"), None)
    end = next((d["value"] for d in dates if d["kind"] == "contract_end"), None)
    supplier = next((o["name"] for o in organizations if o["role"] == "поставщик" and o.get("name")), None)
    summary = ". ".join(x for x in [
        title + (f" (реестровый № {reg_num})" if reg_num else ""),
        f"Предмет: {subject}" if subject else None,
        f"Цена: {_money_str(price)} руб" if price is not None else None,
        f"Поставщик: {supplier}" if supplier else None,
        f"Срок исполнения: до {end}" if end else None,
    ] if x) + "."

    raw_data = {
        "document_name": {"value": title, "confidence": 1.0},
        "summary": summary,
        "contract_number": {"value": number or reg_num, "evidence": _ev("Номер контракта", number) if number else None},
        "registry_number": reg_num,
        "purchase_number": purchase_number,
        "ikz": order.get("purchaseCode") if isinstance(order, dict) else None,
        "placing_method": order.get("placingSingleCustomerText") if isinstance(order, dict) else None,
        "dates": dates,
        "finances": finances,
        "organizations": organizations,
        "procurement_subject": {"description": subject or "", "evidence": _ev("Предмет контракта", subject) if subject else None,
                                "items": []},
        "items": items,
        "items_total": len(_all(body, "products", "product")),
        "execution_location": {"address": address, "special_conditions": _cap(place, 1000) or "",
                               "evidence": _ev("Место поставки", address) if address else None},
        "warranty": _cap(warranty, 1000),
        "source": "print_form_xml",
    }
    extraction = {"readability": {"status": "allow", "readability_score": 1.0, "main_language": "ru", "issues": []},
                  "raw_data": raw_data}
    key_conditions = [{"name": d["context"], "value": d["value"], "section": SECTION} for d in dates] + \
                     [{"name": f["context"], "value": (f"{_money_str(f['value'])} {f['currency']}" if f["currency"] != "%"
                                                       else f"{f['value']:g} %"), "section": SECTION} for f in finances]
    profile = {
        "resume": summary,
        "purpose": subject or title,
        "structure": {"main_sections": [], "has_tables": bool(items), "has_appendices": False, "appendices": []},
        "key_conditions": key_conditions,
        "semantics": {"topics": [], "keywords": [], "classification": {}},
        "technical_objects": {"brands": [], "models": [], "manufacturers": [], "standards": []},
        "assessment": {},
        "recommendations": [],
        "confidence": 1.0,
        "source": "print_form_xml",
    }
    return extraction, profile


def is_contract_xml(body: Any) -> bool:
    """Похоже ли тело XML на контракт 44-ФЗ (есть номер или реестровый номер и цена или предмет)."""
    return isinstance(body, dict) and bool(body.get("regNum") or body.get("number")) \
        and bool(_get(body, "priceInfo", "price") or body.get("contractSubject"))


def is_stale(ai_analysis: Optional[Dict[str, Any]], row: Dict[str, Any]) -> bool:
    """Сохранённый паспорт печатной формы контракта, собранный по тексту правилами извещения (до разбора по XML).

    Такой результат пересобирается (без модели), а не дозаписывается как есть. Если XML при пересборке не найдётся,
    паспорт снова строится по тексту — повторная пересборка дешёвая.

    Args:
        ai_analysis: Результат из ``ai_rm_results``.
        row: Строка ``files_meta.sql``.

    Returns:
        bool: ``True`` — результат печатной формы контракта с ``raw_data.source = "print_form"`` и файл связан с XML.
    """
    raw = (ai_analysis or {}).get("raw_data") if isinstance(ai_analysis, dict) else None
    if not isinstance(raw, dict) or raw.get("source") != "print_form" or not row.get("xml_source_id"):
        return False
    from risk_monitoring import doc_kind

    kind = doc_kind.print_form_kind(row) if doc_kind.is_print_form(row) else None
    return bool(kind) and kind.get("group") == "contract"

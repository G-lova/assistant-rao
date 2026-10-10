"""Событие ЕИС из XML-документа: разбор, сравнение версий, классификация, сведения, вложения.

Чистые функции без сети и БД (их покрывают тесты ``tests/test_rm_xml_events.py``). Детерминированное
считает код (концепция, гл. 9, 11): тип события по тегу и версии, изменения «было → стало» с процентом
изменения, подтипы изменений (НМЦК, сроки, обеспечение, документация, доп. соглашение …), изменения
приложенных файлов, ключевые сведения (номера, ИКЗ, суммы, даты, стороны) и привязку «сирот» по ИКЗ.
LLM (``EventAnalyzer``) получает уже подготовленный пакет и оценивает значимость и риски.
"""
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from risk_monitoring import risk_catalog
from risk_monitoring.passport import merge_risks, now_iso, total_risk, SCHEMA_VERSION_XML

MAX_XML_BYTES = 30 * 1024 * 1024
MAX_CHANGES_STORED = 200
MAX_CHANGES_LLM = 60

# Служебные поля, изменение которых не является событием
IGNORED_KEYS = {
    "id", "externalId", "versionNumber", "docNumber", "fileSize", "publishedContentId", "createDate", "modifyDate",
    "uploadDate", "exportDate", "schemaVersion", "schemeVersion", "guid", "uuid", "versionGUID", "cryptoSigns",
    "fileHash", "signature", "serviceSigns", "printFormInfo", "extPrintFormInfo", "printForm", "extPrintForm",
    "docPublishDTInEIS", "publishDTInEIS", "publishDate", "docPublishDate", "href", "contentId",
}
# Поля, по которым элементы повторяющихся блоков сопоставляются между версиями
LIST_KEY_FIELDS = ("sid", "externalSid", "ordinalNumber", "stageOrdinalNumber", "regNum", "code", "fileName", "url")
DICT_KEY_FIELDS = ("sid", "externalSid", "ordinalNumber", "stageOrdinalNumber")

FIELD_LABELS = {
    "maxPrice": "НМЦК", "price": "Цена", "priceRUR": "Цена, руб.", "priceRub": "Цена, руб.",
    "currency": "Валюта", "endDT": "Окончание подачи заявок", "startDT": "Начало подачи заявок",
    "summarizingDate": "Дата подведения итогов", "biddingDate": "Дата проведения торгов",
    "firstPartsDate": "Дата рассмотрения первых частей", "secondPartsDate": "Дата рассмотрения вторых частей",
    "endDate": "Дата окончания", "startDate": "Дата начала", "signDate": "Дата подписания",
    "executionPeriod": "Срок исполнения", "advancePaymentSum": "Аванс", "advancePaymentPercents": "Аванс, %",
    "amount": "Сумма", "part": "Размер, %", "purchaseObjectInfo": "Предмет закупки",
    "contractSubject": "Предмет контракта", "fileName": "Файл", "docDescription": "Описание документа",
    "url": "Ссылка", "quantity": "Количество", "name": "Наименование", "code": "Код",
}

# (шаблон пути в нижнем регистре, код подтипа) — для извещений
NOTICE_SUBTYPES: List[Tuple[str, str]] = [
    (r"maxprice", "PUR-003"),
    (r"(guarantee|advance|обесп)", "PUR-014"),
    (r"(enddt|startdt|summarizingdate|biddingdate|firstpartsdate|secondpartsdate|collectinginfo|procedureinfo)", "PUR-004"),
    (r"attachment", "PUR-005"),
]
# — для контрактов
CONTRACT_SUBTYPES: List[Tuple[str, str]] = [
    (r"modification", "CON-004"),
    (r"(priceinfo|(^|\.)price(rur|rub)?$|\.price\b)", "CON-002"),
    (r"(executionperiod|enddate|startdate|executiondate|stages?\b|stage\[)", "CON-003"),
    (r"(supplier|participant)", "CON-008"),
]

KEY_RE = re.compile(r"^(?:\d{19}|\d{18,20})$")


# ------------------------------------------------------------------------------ разбор

def _local(key: str) -> str:
    """Локальное имя тега без префикса пространства имён и ``@`` атрибута."""
    key = key.lstrip("@")
    return key.split(":", 1)[1] if ":" in key else key


def normalize(node: Any) -> Any:
    """Приводит результат ``xmltodict`` к простому виду: без namespace, без ``xmlns``-атрибутов,
    ``{"#text": v}`` → ``v``, строки без крайних пробелов.

    Args:
        node: Узел ``xmltodict``.

    Returns:
        Any: Нормализованный узел.
    """
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if k.startswith("@xmlns") or k.startswith("xmlns") or k.startswith("@xsi") or k.startswith("@schema"):
                continue
            name = _local(k)
            out[name] = normalize(v)
        if set(out) == {"#text"}:
            return out["#text"]
        if "#text" in out and all(k == "#text" or not isinstance(out[k], (dict, list)) for k in out):
            return out["#text"]
        return out
    if isinstance(node, list):
        return [normalize(v) for v in node]
    if isinstance(node, str):
        return node.strip()
    return node


def parse_xml(raw: bytes) -> Tuple[str, Optional[int], Dict[str, Any]]:
    """Разбирает XML ЕИС: тег документа, номер версии из XML и нормализованное тело.

    Внешний ``<export>`` снимается. Внешние сущности не раскрываются (``xmltodict`` с
    ``disable_entities=True``), размер ограничен ``MAX_XML_BYTES``.

    Args:
        raw: Байты XML.

    Returns:
        Tuple[str, Optional[int], Dict[str, Any]]: ``(тег, versionNumber, тело)``.

    Raises:
        ValueError: Пустой, слишком большой или некорректный XML.
    """
    import xmltodict

    if not raw:
        raise ValueError("пустой XML")
    if len(raw) > MAX_XML_BYTES:
        raise ValueError(f"XML больше {MAX_XML_BYTES} байт")
    if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw[:4096]:
        raise ValueError("XML с DTD/сущностями не принимается")
    try:
        data = xmltodict.parse(raw, process_namespaces=False, disable_entities=True)
    except Exception as e:  # noqa: BLE001 — ExpatError и пр.
        raise ValueError(f"некорректный XML: {e}") from e
    data = normalize(data)
    if not isinstance(data, dict) or not data:
        raise ValueError("пустой XML")
    tag, body = next(iter(data.items()))
    if tag == "export" and isinstance(body, dict):
        inner = [(k, v) for k, v in body.items() if isinstance(v, dict)]
        if inner:
            tag, body = inner[0]
    body = body if isinstance(body, dict) else {"value": body}
    version = body.get("versionNumber")
    try:
        version = int(version) if version not in (None, "") else None
    except (TypeError, ValueError):
        version = None
    return tag, version, body


def strip_ignored(node: Any) -> Any:
    """Удаляет служебные поля (``IGNORED_KEYS``) на всех уровнях.

    Args:
        node: Узел.

    Returns:
        Any: Копия без служебных полей.
    """
    if isinstance(node, dict):
        return {k: strip_ignored(v) for k, v in node.items() if k not in IGNORED_KEYS}
    if isinstance(node, list):
        return [strip_ignored(v) for v in node]
    return node


# ------------------------------------------------------------------------- сравнение

def _item_key(item: Any, fields: Sequence[str]) -> Optional[str]:
    """Ключ элемента повторяющегося блока: ``поле=значение`` или ``None``."""
    if isinstance(item, dict):
        for f in fields:
            v = item.get(f)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return f"{f}={v}"
    return None


def _hash(item: Any) -> str:
    """Короткий хэш содержимого (ключ элемента без естественного ключа)."""
    return "#" + hashlib.sha1(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:8]


def flatten(node: Any, path: str = "", out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Плоское представление «путь → значение» для сравнения версий.

    Повторяющиеся блоки индексируются естественным ключом (``sid``, ``ordinalNumber``, ``code``,
    ``url`` …), а не позицией: перестановка или вставка элемента не порождает ложных изменений.
    Одиночный элемент с ключом (``sid``, ``ordinalNumber``) индексируется так же, как в списке, —
    переход «один элемент → список» между версиями не даёт шума.

    Args:
        node: Узел.
        path: Путь родителя.
        out: Накопитель.

    Returns:
        Dict[str, Any]: Пути и скалярные значения.
    """
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, dict) and _item_key(v, DICT_KEY_FIELDS):
                flatten([v], p, out)
            else:
                flatten(v, p, out)
    elif isinstance(node, list):
        if all(not isinstance(x, (dict, list)) for x in node):
            out[path] = "; ".join(sorted(str(x) for x in node if x not in (None, "")))
        else:
            used: Dict[str, int] = {}
            for item in node:
                key = _item_key(item, LIST_KEY_FIELDS) or _hash(item)
                if key in used:  # одинаковый ключ у разных элементов — различаем содержимым
                    used[key] += 1
                    key = f"{key}{_hash(item)}"
                else:
                    used[key] = 1
                flatten(item, f"{path}[{key}]", out)
    else:
        out[path] = node
    return out


def _num(v: Any) -> Optional[float]:
    """Число из значения XML (``"1 000,50"`` → 1000.5) или ``None``."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def label_for(path: str) -> Optional[str]:
    """Человекочитаемое название поля по последнему сегменту пути."""
    last = re.sub(r"\[.*?\]", "", path).split(".")[-1]
    return FIELD_LABELS.get(last)


def diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Изменения между версиями документа.

    Args:
        old: Тело предыдущей версии (после :func:`strip_ignored`).
        new: Тело новой версии.

    Returns:
        List[Dict[str, Any]]: ``path``, ``label``, ``old``, ``new``, ``change`` (added / removed /
        modified), для чисел — ``delta`` и ``delta_pct``. Сначала поля с названиями.
    """
    a, b = flatten(old), flatten(new)
    changes: List[Dict[str, Any]] = []
    for path in sorted(set(a) | set(b)):
        o, n = a.get(path), b.get(path)
        if o == n or (o in (None, "") and n in (None, "")):
            continue
        change = "added" if path not in a else "removed" if path not in b else "modified"
        item: Dict[str, Any] = {"path": path, "label": label_for(path), "old": o, "new": n, "change": change}
        no, nn = _num(o), _num(n)
        if change == "modified" and no is not None and nn is not None:
            item["delta"] = round(nn - no, 2)
            item["delta_pct"] = round((nn - no) / no * 100, 2) if no else None
        changes.append(item)
    changes.sort(key=lambda c: (c["label"] is None, c["path"]))
    return changes


# --------------------------------------------------------------------------- сведения

def walk(node: Any, path: str = "") -> Iterator[Tuple[str, str, Any]]:
    """Обход дерева: ``(путь, имя поля, значение)`` для каждого поля."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            yield p, k, v
            yield from walk(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


def find_values(body: Dict[str, Any], names: Iterable[str], path_re: Optional[str] = None) -> List[Tuple[str, Any]]:
    """Все скалярные значения полей с указанными именами (с фильтром по пути).

    Args:
        body: Тело документа.
        names: Имена полей.
        path_re: Регулярное выражение для пути (без учёта регистра).

    Returns:
        List[Tuple[str, Any]]: ``(путь, значение)``.
    """
    names = set(names)
    rx = re.compile(path_re, re.I) if path_re else None
    return [(p, v) for p, k, v in walk(body) if k in names and not isinstance(v, (dict, list))
            and v not in (None, "") and (rx is None or rx.search(p))]


def _first(body: Dict[str, Any], names: Iterable[str], path_re: Optional[str] = None,
           pred=None) -> Optional[Any]:
    """Первое значение поля (с фильтром по пути и предикату значения)."""
    for _, v in find_values(body, names, path_re):
        if pred is None or pred(str(v)):
            return v
    return None


def parse_ikz(ikz: Optional[str]) -> Optional[Dict[str, str]]:
    """Разбор ИКЗ (36 цифр, приказ Минфина № 567н).

    Позиции: 1–2 год; 3–22 ИКУ (1 знак уровня + ИНН 10 + КПП 9); 23–26 номер позиции плана-графика;
    27–29 порядковый номер закупки; 30–33 ОКПД2 (4 знака); 34–36 КВР.

    Args:
        ikz: ИКЗ.

    Returns:
        Optional[Dict[str, str]]: Части ИКЗ или ``None``, если формат не совпал.
    """
    s = re.sub(r"\D", "", str(ikz or ""))
    if len(s) != 36:
        return None
    return {"ikz": s, "year": "20" + s[0:2], "iku": s[2:22], "customer_inn": s[3:13], "customer_kpp": s[13:22],
            "plan_position": s[22:26], "purchase_no": s[26:29], "okpd2": s[29:33], "kvr": s[33:36]}


def extract_facts(tag: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Ключевые сведения документа (номера, ИКЗ, стороны, суммы, даты, основания изменения/расторжения).

    Ищутся по именам полей во всём документе: схемы ЕИС разных типов кладут одно и то же в разные места.

    Args:
        tag: Тег документа.
        body: Нормализованное тело (до удаления служебных полей).

    Returns:
        Dict[str, Any]: Найденные сведения (отсутствующие — не включаются).
    """
    f: Dict[str, Any] = {"xml_tag": tag}
    f["purchase_number"] = _first(body, ["purchaseNumber", "notificationNumber"], pred=lambda v: re.fullmatch(r"\d{11,19}", v))
    f["contract_reg_number"] = _first(body, ["regNum", "contractRegNum", "reestrNumber"], pred=lambda v: re.fullmatch(r"\d{19}", v))
    ikz = _first(body, ["IKZ", "ikz", "purchaseCode", "IKZCode"], pred=lambda v: len(re.sub(r"\D", "", v)) == 36)
    if ikz:
        f["ikz"] = re.sub(r"\D", "", str(ikz))
        f["ikz_parts"] = parse_ikz(ikz)
    f["max_price"] = _num(_first(body, ["maxPrice"]))
    f["price"] = _num(_first(body, ["price", "priceRUR"], path_re=r"price"))
    f["currency"] = _first(body, ["code"], path_re=r"currency")
    f["placing_way"] = _first(body, ["name"], path_re=r"placingWay")
    f["placing_way_code"] = _first(body, ["code"], path_re=r"placingWay")
    f["subject"] = _first(body, ["purchaseObjectInfo", "contractSubject", "subject"])
    f["published_at"] = _first(body, ["publishDTInEIS", "docPublishDTInEIS", "publishDate", "docPublishDate"])
    f["sign_date"] = _first(body, ["signDate", "conclusionDate"])
    f["applications_end"] = _first(body, ["endDT"], path_re=r"collecting|procedure")
    f["execution_end"] = _first(body, ["endDate"], path_re=r"execution|period")
    f["customer_reg_num"] = _first(body, ["regNum"], path_re=r"customer", pred=lambda v: re.fullmatch(r"\d{11}", v))
    f["customer_inn"] = _first(body, ["INN", "inn"], path_re=r"customer")
    f["customer_kpp"] = _first(body, ["KPP", "kpp"], path_re=r"customer")
    f["supplier_inn"] = _first(body, ["INN", "inn"], path_re=r"supplier|participant|winner")
    f["supplier_name"] = _first(body, ["fullName", "shortName", "organizationName"], path_re=r"supplier|participant|winner")
    mod = [v for p, k, v in walk(body) if k == "modification" and isinstance(v, (dict, list))]
    if mod:
        m = mod[0] if isinstance(mod[0], dict) else (mod[0][0] if mod[0] else {})
        f["modification_reason_code"] = _first(m, ["code"], path_re=r"reason|base")
        f["modification_reason"] = _first(m, ["name", "info", "description"], path_re=r"reason|base") or _first(m, ["info", "description"])
    term = [v for p, k, v in walk(body) if k in ("termination", "contractTermination") and isinstance(v, dict)]
    if term:
        f["termination_reason_code"] = _first(term[0], ["code"])
        f["termination_reason"] = _first(term[0], ["name", "reason"])
        f["termination_date"] = _first(term[0], ["terminationDate", "date", "paid"])
    f["final_stage"] = str(_first(body, ["finalStageExecution", "isFinalStage", "finalStage"]) or "").lower() == "true" or None
    return {k: v for k, v in f.items() if v not in (None, "", [])}


def attachments(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Приложенные файлы: узлы со ссылкой (``url``) и именем или описанием документа.

    Args:
        body: Нормализованное тело.

    Returns:
        List[Dict[str, Any]]: ``url``, ``file_name``, ``description``, ``content_id``, ``path``.
    """
    out, seen = [], set()

    def visit(node: Any, path: str) -> None:
        """Рекурсивный поиск узлов-вложений."""
        if isinstance(node, dict):
            url = node.get("url")
            if isinstance(url, str) and url and (node.get("fileName") or node.get("docDescription")):
                if url not in seen:
                    seen.add(url)
                    out.append({"url": url, "file_name": node.get("fileName"), "description": node.get("docDescription"),
                                "content_id": node.get("publishedContentId"), "path": path})
            for k, v in node.items():
                visit(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for v in node:
                visit(v, path)

    visit(body, "")
    return out


def document_changes(old: Optional[List[Dict[str, Any]]], new: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Изменения приложенных файлов между версиями: добавлены, удалены, заменены.

    Замена — удалённый и добавленный файл с тем же именем или описанием.

    Args:
        old: Вложения предыдущей версии (``None`` — предыдущей версии нет: все файлы новые).
        new: Вложения новой версии.

    Returns:
        Dict[str, List]: ``added``, ``removed``, ``replaced`` (``{"before", "after"}``).
    """
    old = old or []
    old_urls, new_urls = {a["url"] for a in old}, {a["url"] for a in new}
    added = [a for a in new if a["url"] not in old_urls]
    removed = [a for a in old if a["url"] not in new_urls]
    replaced = []
    for r in list(removed):
        key = (r.get("file_name") or "").lower(), (r.get("description") or "").lower()
        for a in list(added):
            if (key[0] and key[0] == (a.get("file_name") or "").lower()) or (key[1] and key[1] == (a.get("description") or "").lower()):
                replaced.append({"before": r, "after": a})
                removed.remove(r)
                added.remove(a)
                break
    return {"added": added, "removed": removed, "replaced": replaced}


# ------------------------------------------------------------------------ классификация

def subtypes(tag: str, changes: Sequence[Dict[str, Any]]) -> List[str]:
    """Подтипы события по путям изменений (концепция, гл. 11.4–11.5).

    Args:
        tag: Тег документа.
        changes: Изменения (:func:`diff`).

    Returns:
        List[str]: Коды подтипов без повторов, в порядке важности.
    """
    rules = NOTICE_SUBTYPES if tag in risk_catalog.NOTICE_TAGS else CONTRACT_SUBTYPES if tag in risk_catalog.CONTRACT_TAGS else []
    found: List[str] = []
    for c in changes:
        p = c["path"].lower()
        for rx, code in rules:
            if re.search(rx, p) and code not in found:
                found.append(code)
                break
    order = risk_catalog.IMPORTANCE_ORDER
    found.sort(key=lambda code: -order.index(risk_catalog.event_card(code)["importance"]))
    return found


def classify(tag: str, version: Optional[int], has_previous: bool, changes: Sequence[Dict[str, Any]],
             facts: Dict[str, Any], doc_changes: Optional[Dict[str, List]] = None) -> Dict[str, Any]:
    """Тип события по тегу корня, версии и изменениям.

    Извещение версии 0 — «Опубликована закупка»; больше 0 — «Изменена закупка» с подтипами
    (НМЦК, сроки, обеспечение/аванс, документация). Контракт версии 0 — «Заключён контракт»;
    больше 0 — изменение с подтипами (стоимость, сроки, доп. соглашение). Сведения об исполнении —
    частичное или полное исполнение, расторжение. Остальные теги — по таблице «Верхние теги».

    Args:
        tag: Тег документа.
        version: Версия документа ЕИС (``eis_version``; первая версия — 0).
        has_previous: Найдена ли предыдущая версия в БД.
        changes: Изменения относительно предыдущей версии.
        facts: Сведения (:func:`extract_facts`).
        doc_changes: Изменения вложений.

    Returns:
        Dict[str, Any]: ``code``, ``title``, ``group``, ``importance``, ``subtypes``, ``first_version``,
        ``previous_missing``, ``xml_tag``, ``eis_version``.
    """
    first = version == 0 or (version is None and not has_previous)
    if tag in risk_catalog.NOTICE_TAGS:
        base = "PUR-001" if first else "PUR-002"
    elif tag in risk_catalog.CONTRACT_TAGS:
        base = "CON-001" if first else "CON-008"
    else:
        base = risk_catalog.TAG_EVENTS.get(tag, "OTH-000")
    if tag in ("contractProcedure", "pprf615ContractProcedure", "performanceContract"):
        if facts.get("termination_reason_code") or facts.get("termination_reason") or facts.get("termination_date"):
            base = "CON-007"
        elif facts.get("final_stage"):
            base = "CON-006"

    subs = subtypes(tag, changes) if not first else []
    if tag in risk_catalog.CONTRACT_TAGS and not first and facts.get("modification_reason_code") and "CON-004" not in subs:
        subs.insert(0, "CON-004")
    if tag in risk_catalog.NOTICE_TAGS and not first and doc_changes and any(doc_changes.values()) and "PUR-005" not in subs:
        subs.append("PUR-005")

    code = subs[0] if subs and base in ("PUR-002", "CON-008") else base
    card = risk_catalog.event_card(code)
    importance = card["importance"]
    for c in changes:
        if (c.get("label") in ("НМЦК", "Цена", "Цена, руб.")) and abs(c.get("delta_pct") or 0) >= 10:
            importance = risk_catalog.max_importance(importance, "high")
    return {
        "code": card["code"],
        "title": card["title"],
        "group": card["group"],
        "importance": importance,
        "subtypes": [{"code": s, "title": risk_catalog.event_card(s)["title"]} for s in subs],
        "first_version": bool(first),
        "previous_missing": bool(not first and not has_previous),
        "xml_tag": tag,
        "eis_version": version,
    }


def _risk(code: str, description: str, level: float, evidence: List[Dict[str, Any]],
          verification: List[str]) -> Dict[str, Any]:
    """Риск детерминированного правила во внешнем формате."""
    return {"rule_id": code, "title": risk_catalog.title(code), "description": description, "level": level,
            "confidence": 1.0, "law": risk_catalog.default_law(code), "evidence": evidence,
            "verification_needed": verification, "source": "rule"}


def rule_risks(event: Dict[str, Any], changes: Sequence[Dict[str, Any]], facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Риски, которые выставляет код (без LLM) по типу события и изменениям.

    Args:
        event: Результат :func:`classify`.
        changes: Изменения.
        facts: Сведения.

    Returns:
        List[Dict[str, Any]]: Риски во внешнем формате (``source = rule``, уверенность 1.0).
    """
    risks: List[Dict[str, Any]] = []
    codes = {event["code"]} | {s["code"] for s in event.get("subtypes", [])}
    price_changes = [c for c in changes if c.get("label") in ("НМЦК", "Цена", "Цена, руб.") and c.get("delta_pct") is not None]
    ev = [{"path": c["path"], "before": c["old"], "after": c["new"], "delta_pct": c.get("delta_pct")} for c in price_changes[:3]]

    if "PUR-003" in codes and price_changes:
        d = max(abs(c["delta_pct"]) for c in price_changes)
        if d >= 10:
            risks.append(_risk("FIN-007", f"НМЦК изменена на {d:.1f} %", 0.8 if d >= 25 else 0.6, ev,
                               ["Проверить обоснование изменения НМЦК и обновлённое обоснование цены"]))
    if "CON-002" in codes and price_changes:
        d = max(abs(c["delta_pct"]) for c in price_changes)
        reason = facts.get("modification_reason")
        risks.append(_risk("CTR-001", f"Цена контракта изменена на {d:.1f} %" + (f" (основание: {reason})" if reason else ""),
                           0.8 if d >= 10 else 0.5, ev,
                           ["Проверить основание изменения цены по ст. 95 44-ФЗ и наличие доп. соглашения"]))
    if "CON-003" in codes:
        risks.append(_risk("CTR-003", "Изменены сроки исполнения контракта", 0.5,
                           [{"path": c["path"], "before": c["old"], "after": c["new"]} for c in changes
                            if re.search(r"date|period|stage", c["path"], re.I)][:3],
                           ["Проверить основание переноса сроков и применение неустоек"]))
    if "CON-004" in codes:
        code = str(facts.get("modification_reason_code") or "")
        target = "CTR-002" if code in ("012", "051") else "CTR-005" if code == "099" else "CTR-004"
        risks.append(_risk(target, f"Дополнительное соглашение к контракту" + (f", основание {code}: {facts.get('modification_reason') or ''}" if code else ""),
                           0.5, [{"modification_reason_code": code or None, "modification_reason": facts.get("modification_reason")}],
                           ["Проверить соответствие основания изменения ст. 95 44-ФЗ"]))
    if event["code"] in ("CON-007", "CON-009"):
        risks.append(_risk("CTR-009", event["title"] + (f": {facts.get('termination_reason')}" if facts.get("termination_reason") else ""),
                           0.8, [{"termination_reason_code": facts.get("termination_reason_code"),
                                  "termination_reason": facts.get("termination_reason"),
                                  "termination_date": facts.get("termination_date")}],
                           ["Проверить причины расторжения или отказа и включение поставщика в РНП"]))
    if event["code"] == "PUR-013" or event["code"] == "CON-012":
        risks.append(_risk("PROC-007", event["title"], 0.7, [{"xml_tag": event["xml_tag"]}],
                           ["Проверить, кому перешло право заключения контракта и цену второго участника"]))
    if event["code"] == "PUR-012":
        risks.append(_risk("PROC-001", event["title"], 0.6, [{"xml_tag": event["xml_tag"]}],
                           ["Проверить, не ограничивала ли документация круг участников"]))
    if event["code"] in ("PUR-008", "PUR-016"):
        risks.append(_risk("PROC-002", event["title"], 0.5, [{"xml_tag": event["xml_tag"]}],
                           ["Проверить причины отмены и наличие повторной закупки"]))
    if event["code"] == "CMP-001":
        risks.append(_risk("PROC-006", event["title"], 0.6, [{"xml_tag": event["xml_tag"]}],
                           ["Изучить доводы жалобы и решение контрольного органа"]))
    return risks


# ---------------------------------------------------------------------- привязка «сирот»

def canonical_org(rows: Sequence[Dict[str, Any]], current_year: int) -> Optional[Dict[str, Any]]:
    """Каноническая строка организации из нескольких (по годам и дублям).

    Порядок: год (текущий, затем ближайший прошлый) → ``monitoring = 1`` → ``status_id = 1`` →
    заполненный ``source`` → минимальный ``id``.

    Args:
        rows: Строки ``risk_monitoring_organisations`` с одинаковыми ИНН/КПП.
        current_year: Текущий год.

    Returns:
        Optional[Dict[str, Any]]: Выбранная строка или ``None``.
    """
    def key(r: Dict[str, Any]):
        """Ключ сортировки (меньше — лучше)."""
        year = int(r.get("year") or 0)
        return (year > current_year, -year, -int(r.get("monitoring") or 0), int(r.get("status_id") or 0) != 1,
                not r.get("source"), int(r.get("id") or 0))
    rows = [r for r in rows if r]
    return sorted(rows, key=key)[0] if rows else None


def pick_by_ikz(cards: Sequence[Dict[str, Any]], event_date: Optional[str]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Выбор карточки закупки по ИКЗ: повторная закупка может иметь тот же ИКЗ.

    Берётся последняя закупка, опубликованная не позже события (если дата известна), иначе последняя.

    Args:
        cards: Карточки с тем же ИКЗ (``id``, ``number``, ``date_public``).
        event_date: Дата события (ISO; берутся первые 10 символов).

    Returns:
        Tuple[Optional[Dict], bool]: ``(карточка, неоднозначно)``.
    """
    if not cards:
        return None, False
    day = (event_date or "")[:10]
    pool = [c for c in cards if not day or not c.get("date_public") or str(c["date_public"])[:10] <= day] or list(cards)
    pool.sort(key=lambda c: (str(c.get("date_public") or ""), int(c.get("id") or 0)), reverse=True)
    return pool[0], len(cards) > 1


# --------------------------------------------------------------------- пакет и результат

def _short(v: Any, n: int = 300) -> Any:
    """Обрезка длинных строк для пакета LLM."""
    return v[:n] + "…" if isinstance(v, str) and len(v) > n else v


def build_llm_payload(event: Dict[str, Any], facts: Dict[str, Any], changes: Sequence[Dict[str, Any]],
                      doc_changes: Dict[str, List], files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Компактный пакет события для ``EventAnalyzer``.

    Args:
        event: Тип события.
        facts: Сведения.
        changes: Изменения (в пакет — до ``MAX_CHANGES_LLM``, сначала поля с названиями).
        doc_changes: Изменения вложений.
        files: Файлы события с кратким анализом (``file_id``, ``file_name``, ``detected_type``, ``resume``).

    Returns:
        Dict[str, Any]: Пакет.
    """
    return {
        "event": {k: event.get(k) for k in ("code", "title", "subtypes", "first_version", "previous_missing", "xml_tag", "eis_version")},
        "facts": {k: _short(v) for k, v in facts.items() if k != "ikz_parts"},
        "changes": [{k: _short(c.get(k)) for k in ("label", "path", "old", "new", "change", "delta_pct") if c.get(k) is not None}
                    for c in list(changes)[:MAX_CHANGES_LLM]],
        "changes_total": len(changes),
        "documents": {k: [{"file_name": (d.get("after") or d).get("file_name") if isinstance(d, dict) else None,
                           "description": (d.get("after") or d).get("description") if isinstance(d, dict) else None}
                          for d in v[:20]] for k, v in (doc_changes or {}).items()},
        "files": [{k: _short(f.get(k), 600) for k in ("file_id", "file_name", "detected_type", "resume") if f.get(k)} for f in files[:15]],
    }


def to_external_event_risks(llm_risks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Риски из ответа ``EventAnalyzer`` → внешний формат.

    Args:
        llm_risks: Риски модели (``rule_id``, ``title``, ``description``, ``level``, ``confidence``, ``law``, ``verification_needed``).

    Returns:
        List[Dict[str, Any]]: Риски с ``source = ai_event``.
    """
    from risk_monitoring.passport import clamp01

    out = []
    for r in llm_risks or []:
        code = r.get("rule_id")
        if code not in risk_catalog.RISKS:
            continue
        out.append({"rule_id": code, "title": r.get("title") or risk_catalog.title(code),
                    "description": r.get("description") or "", "level": clamp01(r.get("level")) or 0.5,
                    "confidence": clamp01(r.get("confidence")) or 0.0,
                    "law": r.get("law") or risk_catalog.default_law(code), "evidence": [],
                    "verification_needed": list(r.get("verification_needed") or []), "source": "ai_event"})
    return out


def build_xml_analysis(*, event: Dict[str, Any], facts: Dict[str, Any], changes: Sequence[Dict[str, Any]],
                       doc_changes: Dict[str, List], files: Sequence[Dict[str, Any]], link: Optional[Dict[str, Any]],
                       rule_risk_list: Sequence[Dict[str, Any]], llm: Optional[Dict[str, Any]],
                       previous: Optional[Dict[str, Any]], pipeline: Dict[str, Any], model: Optional[str],
                       all_files: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Собирает ``ai_analysis`` XML-документа (событие ЕИС).

    Args:
        event: Тип события.
        facts: Сведения.
        changes: Изменения относительно предыдущей версии.
        doc_changes: Изменения вложений.
        files: Файлы текущей версии события (id и краткий анализ).
        link: Предложенная привязка «сироты» (или ``None``).
        rule_risk_list: Риски детерминированных правил.
        llm: Ответ ``EventAnalyzer`` (или ``None``).
        previous: Предыдущая версия XML (``id``, ``eis_version``) или ``None``.
        pipeline: Блок ``pipeline``.
        model: Модель LLM.
        all_files: Файлы текущей и предыдущей версий (для id заменённых и удалённых вложений).

    Returns:
        Dict[str, Any]: ``ai_analysis`` для ``PATCH /api/risk-monitoring/ai-analysis``.
    """
    llm = llm or {}
    lookup = list(all_files) if all_files is not None else list(files)
    risks = merge_risks(list(rule_risk_list or []), to_external_event_risks(llm.get("risks") or []))
    event_out = dict(event)
    event_out["event_at"] = facts.get("published_at") or facts.get("sign_date")
    event_out["previous_xml_id"] = int(previous["id"]) if previous else None
    event_out["previous_eis_version"] = previous.get("eis_version") if previous else None
    if llm.get("significance") is not None:
        event_out["significance"] = llm.get("significance")

    def _file_id(att: Dict[str, Any]) -> Optional[int]:
        """id файла по ссылке вложения."""
        for f in lookup:
            if f.get("source_url") == att.get("url"):
                return f.get("file_id")
        return None

    docs = {
        "added": [{"file_id": _file_id(a), "file_name": a.get("file_name"), "description": a.get("description"), "url": a["url"]}
                  for a in doc_changes.get("added", [])],
        "removed": [{"file_id": _file_id(a), "file_name": a.get("file_name"), "description": a.get("description"), "url": a["url"]}
                    for a in doc_changes.get("removed", [])],
        "replaced": [{"before_file_id": _file_id(r["before"]), "after_file_id": _file_id(r["after"]),
                      "file_name": r["after"].get("file_name"), "before_url": r["before"]["url"], "after_url": r["after"]["url"]}
                     for r in doc_changes.get("replaced", [])],
    }
    result = {
        "schema_version": SCHEMA_VERSION_XML,
        "status": "completed",
        "event": event_out,
        "facts": facts,
        "changes": list(changes)[:MAX_CHANGES_STORED],
        "changes_total": len(changes),
        "changes_assessment": llm.get("changes") or [],
        "documents": docs,
        "files": [{"file_id": f.get("file_id"), "file_name": f.get("file_name"), "detected_type": f.get("detected_type"),
                   "total_doc_risk": f.get("total_doc_risk")} for f in files],
        "resume": llm.get("event_summary") or default_summary(event, facts, changes),
        "confidence": llm.get("confidence") if llm.get("confidence") is not None else (1.0 if not llm else 0.0),
        "risks": risks,
        "total_event_risk": total_risk(risks),
        "model": model,
        "analyzed_at": now_iso(),
        "pipeline": pipeline,
    }
    if link:
        result["link"] = link
    if llm.get("error"):
        result["llm_error"] = llm["error"]
    return result


def default_summary(event: Dict[str, Any], facts: Dict[str, Any], changes: Sequence[Dict[str, Any]]) -> str:
    """Резюме события без LLM (если модель недоступна или событие незначимое).

    Args:
        event: Тип события.
        facts: Сведения.
        changes: Изменения.

    Returns:
        str: Одно-два предложения.
    """
    parts = [event["title"]]
    num = facts.get("purchase_number") or facts.get("contract_reg_number")
    if num:
        parts[0] += f" № {num}"
    labeled = [c for c in changes if c.get("label")][:3]
    if labeled:
        parts.append("Изменения: " + "; ".join(
            f"{c['label']}: {c.get('old')} → {c.get('new')}" + (f" ({c['delta_pct']:+.1f} %)" if c.get("delta_pct") is not None else "")
            for c in labeled))
    return ". ".join(parts) + "."

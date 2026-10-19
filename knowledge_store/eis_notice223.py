"""Правила раздела 1 («Наличие информации…») по XML извещения ЕИС для 223-ФЗ.

XML извещений 223-ФЗ (``purchaseNotice``, ``purchaseNoticeAE``, ``purchaseNoticeEP``, ``purchaseNoticeKESMBO`` …) устроен иначе, чем
``epNotification*`` в 44-ФЗ, поэтому правила 44-ФЗ (:mod:`knowledge_store.eis_notice`) к нему неприменимы: названия
критериев совпадают по номеру (1.6 «количество» — это 1.13 в конкурсе 44-ФЗ), но не по смыслу.

В XML 223-ФЗ есть данные только для критериев 1.1–1.10 формы (заказчик, адреса, способ, предмет, количество, единица,
место поставки, контакты). Требования об обеспечении заявки и договора (1.11–1.14) и ограничения по ст. 3.1-4 (1.15)
в извещении лежат в файле документации, а не в структурированных полях — эти критерии остаются за моделью и экспертом.
У экспертов в данных 223-ФЗ заполнены в основном 1.1–1.8.

Результат — те же :class:`knowledge_store.eis_notice.Finding`, привязка к полям формы — по **номеру** критерия в названии.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence
from xml.etree import ElementTree as ET

from knowledge_store.eis_notice import Finding, _is_placeholder, _local, _safe_et, criterion_number

# Пути относительно элемента ``purchaseNotice*Data``. У лота с несколькими заказчиками (jointLotData) данные лежат глубже.
_JOINT = "lots/lot/jointLotData/lotCustomers/lotCustomer/lotCustomerData"
_ITEM = "lots/lot/lotData/lotItems/lotItem"
_JOINT_ITEM = _JOINT + "/lotItems/lotItem"


@dataclass
class Rule223:
    """Правило критерия раздела 1 для XML 223-ФЗ.

    Attributes:
        criterion: Номер критерия (``1.6``).
        paths: Пути, наличие непустого значения в любом из них даёт ``1``.
        absent: Что вернуть, если значений нет: ``0`` или ``None`` (не решать — XML без структурного поля не доказывает отсутствие).
    """

    criterion: str
    paths: Sequence[str]
    absent: Optional[int] = None


RULES: List[Rule223] = [
    Rule223("1.1", ("customer/mainInfo/fullName", "customer/mainInfo/shortName")),
    Rule223("1.2", ("customer/mainInfo/legalAddress",)),
    Rule223("1.3", ("customer/mainInfo/postalAddress",)),
    Rule223("1.4", ("purchaseCodeName", "purchaseMethodCode")),
    Rule223("1.5", ("lots/lot/lotData/subject", "name")),
    Rule223("1.6", (_ITEM + "/qty", _JOINT_ITEM + "/qty"), absent=0),
    Rule223("1.7", (_ITEM + "/okei/name", _ITEM + "/okei/code", _JOINT_ITEM + "/okei/name"), absent=0),
    Rule223("1.8", ("lots/lot/lotData/deliveryPlace/address", _ITEM + "/deliveryPlace/address", _JOINT + "/deliveryPlace/address",
                    _JOINT_ITEM + "/deliveryPlace/address"), absent=0),
    Rule223("1.9", ("contact/email",)),
    Rule223("1.10", ("contact/phone",)),
]


class Notice223:
    """Обёртка над XML извещения 223-ФЗ: поиск значений по путям из локальных имён тегов."""

    def __init__(self, data: ET.Element):
        """Сохраняет элемент данных извещения (``purchaseNotice*Data``)."""
        self.data = data

    @classmethod
    def from_xml(cls, raw) -> "Notice223":
        """Разбирает XML извещения 223-ФЗ.

        Args:
            raw: ``bytes`` или ``str`` с XML.

        Returns:
            Notice223: Обёртка над элементом ``purchaseNotice*Data``.

        Raises:
            ValueError: XML некорректен или это не извещение 223-ФЗ.
        """
        raw = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        try:
            root = (_safe_et or ET).fromstring(raw)
        except Exception as exc:  # noqa: BLE001 — ParseError и запрещённые конструкции defusedxml
            raise ValueError(f"некорректный XML: {type(exc).__name__}: {exc}") from exc
        for el in root.iter():
            name = _local(el)
            if name.startswith("purchaseNotice") and name.endswith("Data"):
                return cls(el)
        raise ValueError("в XML нет элемента purchaseNotice*Data")

    def elements(self, path: str) -> List[ET.Element]:
        """Элементы по пути из локальных имён от элемента данных извещения."""
        level = [self.data]
        for name in path.split("/"):
            level = [c for e in level for c in e if _local(c) == name]
            if not level:
                return []
        return level

    def texts(self, path: str) -> List[str]:
        """Непустые значения по пути; заглушки (только нули) отбрасываются."""
        out = []
        for el in self.elements(path):
            text = (el.text or "").strip()
            if text and not _is_placeholder(text):
                out.append(text)
        return out

    def version(self) -> int:
        """Номер версии извещения (``version``), 0 если не указан."""
        values = self.texts("version")
        return int(values[0]) if values and values[0].isdigit() else 0


def evaluate_rule(rule: Rule223, notice: Notice223) -> Finding:
    """Применяет правило к извещению: ``1`` с доказательством ``путь = значение``; иначе ``absent`` или ``None``."""
    for path in rule.paths:
        values = notice.texts(path)
        if values:
            return Finding(1, f"{path} = {values[0][:200]}", [path])
    if rule.absent is not None:
        return Finding(rule.absent, "в извещении нет: " + ", ".join(rule.paths)[:480], list(rule.paths))
    return Finding(None, "XML не позволяет решить", [])


def evaluate_all(notice: Notice223) -> Dict[str, Finding]:
    """Применяет все правила раздела 1: ``номер критерия → Finding`` (включая ``value=None``)."""
    return {rule.criterion: evaluate_rule(rule, notice) for rule in RULES}


def findings_to_json(notice: Notice223, findings: Dict[str, Finding]) -> dict:
    """Сериализует результат для ``pe_documents.extraction['eis_notice']`` (формат как у 44-ФЗ, плюс ``law``)."""
    return {"version": notice.version(), "law": "223-ФЗ",
            "criteria": {c: {"value": f.value, "evidence": f.evidence[:500]} for c, f in findings.items() if f.value is not None}}


def map_to_fields(data: dict, fields: Sequence[dict]) -> Dict[str, Finding]:
    """Сопоставляет сохранённые результаты с полями формы 223-ФЗ по номеру в названии критерия (``1.6. Наличие…``).

    Args:
        data: ``extraction['eis_notice']`` (см. :func:`findings_to_json`).
        fields: Поля формы (``field_key``, ``label``, ``value_kind``).

    Returns:
        dict: ``ключ поля → Finding`` для полей ``presence`` раздела 1 с решённым критерием.
    """
    out: Dict[str, Finding] = {}
    criteria = (data or {}).get("criteria", {})
    for item in fields:
        if item.get("value_kind") != "presence" or not str(item.get("field_key", "")).startswith("field2_1_"):
            continue
        found = criteria.get(criterion_number(item.get("label")) or "")
        if found:
            out[item["field_key"]] = Finding(found["value"], found.get("evidence", ""), [])
    return out


def is_notice_223(raw) -> bool:
    """Это XML извещения 223-ФЗ (элемент ``purchaseNotice*Data``)? Быстрая проверка без исключений."""
    try:
        Notice223.from_xml(raw)
        return True
    except ValueError:
        return False


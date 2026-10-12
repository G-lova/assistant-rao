"""Вид документа для риск-мониторинга: печатные формы ЕИС, тип по имени файла, допустимые риски.

* **Печатная форма ЕИС** (``print_form_*.html`` или ссылка ``printForm``) — представление любого
  XML-документа ЕИС: извещения, протокола, сведений о контракте, об исполнении и т. д. Её тип
  определяется по тегу XML-источника (``risk_monitoring_eis_xml_sources``), а не моделью.
* **Тип по имени файла.** Имена вроде «Обоснование начальной максимальной цены контракта.pdf»
  надёжнее модели на плохом OCR. Если тип по имени и тип от детектора расходятся, а детектор
  не уверен (< 0,85), берётся тип по имени.
* **Допустимые коды рисков по типу документа.** Например, в печатной форме извещения ищутся только
  признаки в описании объекта закупки (бренды, характеристики), а обеспечение и аванс — нет: их
  считают правила внешней системы (``pz_p1``, ``pz_p2`` …). В печатных формах протоколов,
  сведений о контракте и исполнении риски в тексте не ищутся — эти данные разбирает анализ XML.

Чистые функции — их покрывают тесты ``tests/test_rm_quality.py``.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from risk_monitoring import risk_catalog

PRINT_FORM_TYPE = "eisPrintForm"
DETECTOR_TRUST = 0.85

# Имя файла → тип документа (по убыванию специфичности)
NAME_TYPES: List[Tuple[re.Pattern, str]] = [(re.compile(p, re.IGNORECASE), t) for p, t in [
    (r"обоснован\w*\s+(н\s*\(?м\)?\s*цк|нмцк|начальн|максимальн|цен)", "docObosnNMCKFiles"),
    (r"коммерческ\w*\s+предложени|ценов\w+\s+информац|запрос\w*\s+(цен|коммерческ)", "docMaterialValidNMCKFiles"),
    (r"описани\w*\s+объекта\s+закупки|техническ\w*\s+задани|(^|[^а-яё])тз([^а-яё]|$)|спецификаци", "docOpusObjectZacupFiles"),
    (r"проект\w*\s+(государственного\s+|муниципального\s+)?(контракта|договора)", "docProjContractFiles"),
    (r"дополнительн\w*\s+соглашени|доп\.?\s*соглашени", "docDopConsentContractFiles"),
    (r"требовани\w*\s+к\s+(содер|состав)[^\n]{0,40}заявк|инструкци\w*\s+по\s+(ее\s+|её\s+)?заполнени", "docTrebContentRequestFiles"),
    (r"порядок\s+рассмотрения\s+и\s+оценки|критери\w*\s+оценки", "docPorViewOcenkFiles"),
    (r"акт\w*\s+(сдачи|при[её]мки|выполненных|оказанных)", "docDocPriemActSdachFiles"),
    (r"(^|[^а-яё])упд([^а-яё]|$)|товарн\w+\s+накладн|торг-12", "docPriemTovSchetFiles"),
    (r"паспорт\s+(изделия|товара)|руководств\w*\s+по\s+эксплуатаци", "docTechDocFiles"),
    (r"сертификат|декларац\w+\s+о\s+соответстви", "docCertValidFiles"),
    (r"(^|[^а-яё])извещени", "docIzvejenieFiles"),
]]

# Допустимые коды рисков по типу документа
_TECH = ["DOC-002", "DOC-003", "DOC-004", "DOC-005", "DOC-006", "DOC-007", "DOC-010",
         "AI-001", "AI-002", "AI-003", "AI-005", "CTR-002"]
_CONTRACT = ["DOC-001", "DOC-002", "DOC-003", "DOC-006", "DOC-007", "DOC-010", "AI-002", "AI-003",
             "FIN-005", "FIN-006", "FIN-007", "FIN-008", "FIN-009", "FIN-012", "FIN-016", "CTR-002"]
_NMCK = ["DOC-007", "DOC-010", "FIN-015"]
_REQUEST = ["DOC-004", "DOC-005", "DOC-006", "DOC-007", "AI-003", "AI-005", "PROC-012"]
_NOTICE = ["DOC-002", "DOC-003", "DOC-004", "DOC-005", "DOC-006", "DOC-007", "DOC-010", "AI-001", "AI-003",
           "AI-005", "FIN-004", "FIN-005", "FIN-006", "FIN-007", "FIN-008", "FIN-009", "PROC-004", "PROC-012"]
# В печатной форме извещения характеристики объекта — обычно позиции КТРУ; «редкость» и «неоднозначность»
# характеристик по ним модель оценить не может (ложные DOC-003 / AI-003 в прогоне 1096) — их ищут в ТЗ.
_NOTICE_PRINT = ["DOC-002", "DOC-005", "DOC-006", "AI-001", "AI-005"]
_ACT = ["DOC-007", "DOC-010"]
_DEFAULT = ["DOC-001", "DOC-002", "DOC-003", "DOC-004", "DOC-005", "DOC-006", "DOC-007", "DOC-010",
            "AI-001", "AI-002", "AI-003", "AI-005", "CTR-002"]

ALLOWED_BY_TYPE: Dict[str, List[str]] = {
    "docOpusObjectZacupFiles": _TECH, "docTechDocFiles": [],
    "docProjContractFiles": _CONTRACT, "docContractPostTovarFiles": _CONTRACT, "docContractDoWorkFiles": _CONTRACT,
    "docContractNIRFiles": _CONTRACT, "docDopConsentContractFiles": _CONTRACT,
    "docObosnNMCKFiles": _NMCK, "docMaterialValidNMCKFiles": _NMCK,
    "docTrebContentRequestFiles": _REQUEST, "docPorViewOcenkFiles": _REQUEST,
    "docIzvejenieFiles": _NOTICE,
    "docDocPriemActSdachFiles": _ACT, "docPriemTovSchetFiles": _ACT, "docCargoTaxFiles": _ACT, "docActPriemTovFiles": _ACT,
    "docCertValidFiles": [], "docPhotoCargoFiles": [], "docPhotoFinishWorkFiles": [],
}
PRINT_FORM_ALLOWED = {"notice": _NOTICE_PRINT}


PRINT_NAME_RE = re.compile(r"^\s*(print[_ ]?form|печатн\w*\s+форм)", re.IGNORECASE)


def is_print_form(row: Dict[str, Any]) -> bool:
    """Печатная форма ЕИС: имя ``print_form*`` / «Печатная форма …» или ссылка на ``printForm``.

    Args:
        row: Строка ``risk_monitoring_files`` (``file_name``, ``source_url``).

    Returns:
        bool: ``True`` для печатной формы.
    """
    url = (row.get("source_url") or row.get("url") or "").lower()
    return bool(PRINT_NAME_RE.search(row.get("file_name") or "")) or "printform" in url


def xml_group(tag: Optional[str], owner_type: Optional[str] = None) -> str:
    """Группа XML-документа ЕИС по тегу корня (в т. ч. без суффикса версии: ``epNotification``).

    Args:
        tag: Тег корня XML или ``xml_source_type``.
        owner_type: Тип владельца файла (``notice`` / ``contract``) — если тег неизвестен.

    Returns:
        str: ``notice`` / ``protocol`` / ``execution`` / ``contract`` / ``other``.
    """
    tag = tag or ""
    t = tag.lower()
    owner = (owner_type or "").lower()
    if "protocol" in t or tag in ("fcsPlacementResult", "fcsProposalsResult"):
        return "protocol"
    if t.startswith(("contractprocedure", "pprf615contractprocedure", "performancecontract")):
        return "execution"
    if tag in risk_catalog.CONTRACT_TAGS or t.startswith("contract"):
        return "contract"
    if tag in risk_catalog.NOTICE_TAGS or re.match(r"(ep|fcs|pprf615)?notification|purchasenotice|notice", t):
        return "notice"
    if owner in ("notice", "contract"):
        return owner
    return "other"


def print_form_kind(row: Dict[str, Any]) -> Dict[str, Any]:
    """Тип печатной формы по тегу XML-источника.

    Тег берётся из XML, к которому привязан файл (``source_xml_tag``, ``source_xml_type``), затем из
    ``xml_source_type`` файла; если тег неизвестен — по владельцу (``owner_type``).

    Args:
        row: Строка ``files_meta.sql``.

    Returns:
        Dict[str, Any]: ``detected_type``, ``type_decode``, ``group`` (notice / protocol / contract /
        execution / other), ``xml_tag``.
    """
    tag = row.get("source_xml_tag") or row.get("source_xml_type") or row.get("xml_source_type") or ""
    group = xml_group(tag, row.get("owner_type"))
    titles = {"notice": "извещение о закупке", "contract": "сведения о контракте (договоре)",
              "execution": "сведения об исполнении контракта", "protocol": "протокол"}
    if tag in risk_catalog.TAG_EVENTS and group in ("protocol", "other"):
        title = risk_catalog.event_card(risk_catalog.TAG_EVENTS[tag])["title"].lower()
    else:
        title = titles.get(group) or tag or "документ ЕИС"
    version = row.get("eis_version")
    changed = group == "notice" and version not in (None, "", 0, "0")
    return {
        "detected_type": "docIzvejenieFiles" if group == "notice" else PRINT_FORM_TYPE,
        "type_decode": f"Печатная форма ЕИС: {title}" + (" (изменение)" if changed else ""),
        "group": group,
        "xml_tag": tag or None,
    }


def type_by_name(file_name: Optional[str]) -> Optional[str]:
    """Тип документа по имени файла (без расширения и нумерации вида «2.»).

    Args:
        file_name: Имя файла.

    Returns:
        Optional[str]: Код типа или ``None``, если имя ничего не говорит.
    """
    name = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", file_name or "")
    name = re.sub(r"^\s*\d+[.)]?\s*", "", name).replace("_", " ").lower()
    for rx, doc_type in NAME_TYPES:
        if rx.search(name):
            return doc_type
    return None


def resolve_type(detector: Optional[Dict[str, Any]], name_type: Optional[str], allowed_types: List[str],
                 fallback: Optional[str] = None) -> Tuple[str, str]:
    """Итоговый тип документа: детектор или имя файла.

    Args:
        detector: Ответ ``TypeDetector`` (``detected_type``, ``confidence``).
        name_type: Тип по имени файла.
        allowed_types: Допустимые коды типов.
        fallback: Тип по названию документа из извлечения (если детектор вернул недопустимый тип).

    Returns:
        Tuple[str, str]: ``(тип, источник)``; источник — ``detector``, ``file_name``, ``detector+file_name``,
        ``document_name``.
    """
    det = (detector or {}).get("detected_type") or ""
    try:
        conf = float((detector or {}).get("confidence"))
    except (TypeError, ValueError):
        conf = None
    if det not in allowed_types:
        det = ""
    if name_type and det == name_type:
        return det, "detector+file_name"
    if name_type and (not det or conf is None or conf < DETECTOR_TRUST):
        return name_type, "file_name"
    if det:
        return det, "detector"
    return (fallback or "docDopMaterialsFiles"), "document_name"


def allowed_codes(detected_type: str, print_group: Optional[str] = None) -> List[str]:
    """Коды рисков, которые ищутся в тексте документа данного типа.

    Args:
        detected_type: Тип документа.
        print_group: Группа печатной формы (``None`` — не печатная форма).

    Returns:
        List[str]: Коды (пустой список — поиск рисков в тексте не проводится).
    """
    if print_group is not None:
        return list(PRINT_FORM_ALLOWED.get(print_group, []))
    return list(ALLOWED_BY_TYPE.get(detected_type, _DEFAULT))

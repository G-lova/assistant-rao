"""Единый каталог рисков и типов событий риск-мониторинга.

Риски (версия ``rm-catalog-2``) читаются из ``risk_monitoring/config/risk_catalog.json``. JSON собирается
скриптом ``scripts/build_risk_catalog.py`` из документа ``docs/risk_catalog.md``, который согласуют
методологи и разработчики внешней системы. Правило каталога: каждому показателю внешней системы
(``pz_p1 … pc_n6``) соответствует ровно один код, сами показатели не меняются. Риски концепции без
показателя (документные, AI, поведенческие, исторические, сетевые) — отдельные коды.

У каждого риска две нормы — по 44-ФЗ и по 223-ФЗ. :func:`default_law` выбирает норму по закону
закупки (``fz_type`` карточки или ``fz`` архива ЕИС). Если закон неизвестен, возвращаются обе.

Коды событий ЕИС (``PUR-*``, ``CON-*`` …) заданы ниже в этом модуле. Контрактные события имеют
префикс ``CON``, чтобы не совпадать с кодами рисков ``CTR-*``.
"""
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

CATALOG_PATH = Path(__file__).resolve().parent / "config" / "risk_catalog.json"

LAW_NAMES = {44: "Федеральный закон № 44-ФЗ", 223: "Федеральный закон № 223-ФЗ"}


def _load() -> Dict[str, Any]:
    """Читает JSON каталога рисков."""
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


_DATA = _load()
CATALOG_VERSION: str = _DATA["version"]

# Код → запись каталога: title, category, kind (risk / violation / ai), stage, indicator, rule_no,
# rule_active, detected_by, law_44, law_223, applicable_223, concept_code
RISKS: Dict[str, Dict[str, Any]] = {r["code"]: r for r in _DATA["risks"]}

# Показатель внешней системы (pz_p1 …) → код каталога
INDICATORS: Dict[str, str] = {r["indicator"]: r["code"] for r in _DATA["risks"] if r.get("indicator")}

# Коды, которые разрешено возвращать LLM при анализе отдельного документа (файла)
DOCUMENT_RISK_CODES: List[str] = [
    "DOC-001", "DOC-002", "DOC-003", "DOC-004", "DOC-005", "DOC-006", "DOC-007", "DOC-010",
    "AI-001", "AI-002", "AI-003", "AI-005",
    "FIN-004", "FIN-005", "FIN-006", "FIN-007", "FIN-008", "FIN-009", "FIN-012", "FIN-015", "FIN-016",
    "PROC-004", "PROC-012",
    "CTR-002",
]

# Риски, которые выставляет сравнение редакций документа (код, не LLM)
VERSION_RISK_CODES: List[str] = ["DOC-011", "DOC-012", "AI-004"]

# Коды, которые разрешено возвращать LLM при анализе XML-события ЕИС
EVENT_RISK_CODES: List[str] = [
    "FIN-003", "FIN-010", "FIN-011", "FIN-013",
    "PROC-001", "PROC-002", "PROC-003", "PROC-005", "PROC-006", "PROC-008", "PROC-009", "PROC-010", "PROC-011",
    "CTR-004", "CTR-005", "CTR-006", "CTR-007", "CTR-008", "CTR-009", "CTR-010", "CTR-011", "CTR-012", "CTR-013",
    "CTR-014", "CTR-015", "CTR-016", "CTR-017", "CTR-018", "CTR-019", "CTR-020", "CTR-021", "CTR-022", "CTR-023",
    "CTR-024",
    "REG-002", "DATA-001", "AI-004", "AI-006",
]

SEVERITY_LEVEL = {"low": 0.3, "medium": 0.6, "high": 0.9}


def normalize_fz(fz: Any) -> Optional[int]:
    """Закон закупки: 44, 223 или ``None``.

    Args:
        fz: Значение ``fz_type`` / ``fz`` (число, строка «44», «223-ФЗ» и т. п.).

    Returns:
        Optional[int]: 44, 223 или ``None``, если закон не распознан.
    """
    try:
        value = int(str(fz).strip().split("-")[0])
    except (TypeError, ValueError):
        return None
    return value if value in LAW_NAMES else None


def category(code: str) -> str:
    """Категория риска по префиксу кода (``DOC-001`` → ``DOC``).

    Args:
        code: Код риска.

    Returns:
        str: Префикс в верхнем регистре или ``OTHER``.
    """
    head = (code or "").split("-")[0].upper()
    return head or "OTHER"


def title(code: str) -> str:
    """Название риска из каталога.

    Args:
        code: Код риска.

    Returns:
        str: Название или сам код, если его нет в каталоге.
    """
    return (RISKS.get(code) or {}).get("title") or code


def kind(code: str) -> Optional[str]:
    """Вид риска: ``risk`` (риск), ``violation`` (признак нарушения), ``ai`` (выявляет только ИИ).

    Args:
        code: Код риска.

    Returns:
        Optional[str]: Вид или ``None`` для кода вне каталога.
    """
    return (RISKS.get(code) or {}).get("kind")


def indicator(code: str) -> Optional[str]:
    """Показатель внешней системы, которому соответствует код (``FIN-004`` → ``pz_p1``).

    Args:
        code: Код риска.

    Returns:
        Optional[str]: Код показателя или ``None``.
    """
    return (RISKS.get(code) or {}).get("indicator")


def applicable(code: str, fz: Any) -> bool:
    """Применим ли риск к закупке по данному закону (ст. 111 44-ФЗ к 223-ФЗ не применяется).

    Args:
        code: Код риска.
        fz: Закон закупки.

    Returns:
        bool: ``False`` только для рисков, явно неприменимых к 223-ФЗ.
    """
    return not (normalize_fz(fz) == 223 and (RISKS.get(code) or {}).get("applicable_223") is False)


def default_law(code: str, fz: Any = None) -> Optional[str]:
    """Нормативное основание риска по закону закупки.

    Args:
        code: Код риска.
        fz: Закон закупки (44 / 223). ``None`` — закон неизвестен: возвращаются нормы обоих законов.

    Returns:
        Optional[str]: Например, «Федеральный закон № 44-ФЗ: ст. 37»; ``None``, если нормы нет.
    """
    rec = RISKS.get(code) or {}
    law_fz = normalize_fz(fz)
    parts = []
    for n in ([law_fz] if law_fz else [44, 223]):
        text = rec.get(f"law_{n}")
        if text:
            parts.append(f"{LAW_NAMES[n]}: {text}")
        elif n == 223 and law_fz == 223 and rec.get("applicable_223") is False:
            parts.append(f"{LAW_NAMES[223]}: не применяется")
    return " | ".join(parts) or None


def catalog_text(codes: Iterable[str]) -> str:
    """Список «код — название» для вставки в промпт.

    Args:
        codes: Коды рисков.

    Returns:
        str: Строки ``- CODE — Название``.
    """
    return "\n".join(f"- {c} — {title(c)}" for c in codes)


# ----------------------------------------------------------------------------- события

def _e(title: str, group: str, importance: str) -> Dict[str, str]:
    """Карточка типа события.

    Args:
        title: Название события.
        group: Группа (концепция, гл. 11.3): ``purchase`` / ``contract`` / ``document`` / ``control`` / ``registry`` / ``report`` / ``other``.
        importance: Важность по умолчанию: ``critical`` / ``high`` / ``medium`` / ``low``.

    Returns:
        dict: ``{"title", "group", "importance"}``.
    """
    return {"title": title, "group": group, "importance": importance}


EVENTS: Dict[str, Dict[str, str]] = {
    # Закупка (концепция, гл. 11.4)
    "PUR-001": _e("Опубликована закупка", "purchase", "medium"),
    "PUR-002": _e("Изменена закупка", "purchase", "medium"),
    "PUR-003": _e("Изменена НМЦК закупки", "purchase", "high"),
    "PUR-004": _e("Изменены сроки закупки", "purchase", "medium"),
    "PUR-005": _e("Изменена документация закупки", "purchase", "medium"),
    "PUR-006": _e("Добавлен документ закупки", "purchase", "low"),
    "PUR-007": _e("Разъяснения по закупке", "purchase", "low"),
    "PUR-008": _e("Отменена закупка", "purchase", "high"),
    "PUR-009": _e("Возобновлена закупка", "purchase", "medium"),
    "PUR-010": _e("Подведены итоги / определён победитель закупки", "purchase", "medium"),
    "PUR-011": _e("Рассмотрены заявки по закупке", "purchase", "low"),
    "PUR-012": _e("Отсутствуют или отозваны заявки", "purchase", "medium"),
    "PUR-013": _e("Уклонение победителя или отклонение заявки", "purchase", "high"),
    "PUR-014": _e("Изменены обеспечение или аванс закупки", "purchase", "high"),
    "PUR-015": _e("Проект контракта опубликован или изменён", "purchase", "low"),
    "PUR-016": _e("Отмена определения поставщика", "purchase", "high"),
    # Контракт (концепция, гл. 11.5; CTR → CON)
    "CON-001": _e("Заключён контракт", "contract", "medium"),
    "CON-002": _e("Изменена стоимость контракта", "contract", "high"),
    "CON-003": _e("Изменены сроки контракта", "contract", "high"),
    "CON-004": _e("Дополнительное соглашение к контракту", "contract", "high"),
    "CON-005": _e("Частичное исполнение контракта", "contract", "low"),
    "CON-006": _e("Полное исполнение контракта", "contract", "medium"),
    "CON-007": _e("Расторжение контракта", "contract", "critical"),
    "CON-008": _e("Изменены сведения о контракте", "contract", "medium"),
    "CON-009": _e("Односторонний отказ или претензионная переписка", "contract", "critical"),
    "CON-010": _e("Электронное актирование по контракту", "contract", "low"),
    "CON-011": _e("Подписание контракта на площадке", "contract", "medium"),
    "CON-012": _e("Уклонение или отказ от заключения контракта", "contract", "high"),
    # Контроль, реестры, отчёты
    "CMP-001": _e("Жалоба или решение по жалобе", "control", "high"),
    "CTL-001": _e("Контрольное мероприятие или проверка", "control", "high"),
    "RNP-001": _e("Сведения реестра недобросовестных поставщиков", "registry", "high"),
    "RPT-001": _e("Отчёт заказчика", "report", "low"),
    "PLN-001": _e("План-график или план закупок", "report", "low"),
    "OTH-000": _e("Прочее событие ЕИС", "other", "low"),
}

NOTICE_TAGS = {
    "epNotificationEZK2020", "epNotificationEF2020", "epNotificationEZT2020", "epNotificationEOK2020",
    "fcsNotificationEP", "fcsNotification111", "pprf615NotificationPO", "pprf615NotificationEF",
    "purchaseNotice", "purchaseNoticeOK", "purchaseNoticeOA", "purchaseNoticeAE", "purchaseNoticeAE94FZ",
    "purchaseNoticeAESMBO", "purchaseNoticeZK", "purchaseNoticeZKESMBO", "purchaseNoticeZPESMBO", "purchaseNoticeEP",
}
CONTRACT_TAGS = {"contract", "pprf615Contract", "contractCutted"}

# Тег корня XML → код события (по «Верхним тегам» и маппингу из xml_processor.get_event_type)
TAG_EVENTS: Dict[str, str] = {
    # закупка
    "epNotificationCancel": "PUR-008", "fcsNotificationCancel": "PUR-008", "pprf615NotificationCancel": "PUR-008",
    "purchaseRejection": "PUR-008", "purchaseLotCancellation": "PUR-008",
    "epNotificationCancelFailure": "PUR-009", "fcsNotificationCancelFailure": "PUR-009",
    "epNoticeApplicationsAbsence": "PUR-012", "epNoticeApplicationCancel": "PUR-012",
    "EpProtocolEOK2020FirstSections": "PUR-011", "epProtocolEOK2020FirstSections": "PUR-011",
    "EpProtocolEOK2020SecondSections": "PUR-011", "epProtocolEOK2020SecondSections": "PUR-011",
    "pprf615ProtocolEF1": "PUR-011", "pprf615ProtocolEF2": "PUR-011", "fcsProtocolEF1": "PUR-011",
    "fcsProtocolEF2": "PUR-011", "epProtocolEF2020SubmitOffers": "PUR-011", "fcsProposalsResult": "PUR-011",
    "epProtocolEOKSingleApp": "PUR-011", "fcsProtocolEFSingleApp": "PUR-011",
    "epProtocolEZK2020Final": "PUR-010", "epProtocolEZT2020Final": "PUR-010", "epProtocolEF2020Final": "PUR-010",
    "epProtocolEOK2020Final": "PUR-010", "pprf615ProtocolPO": "PUR-010", "epProtocolEOK3": "PUR-010",
    "fcsProtocolEF3": "PUR-010", "fcsPlacementResult": "PUR-010", "purchaseProtocol": "PUR-010",
    "epProtocolCancel": "PUR-016", "pprf615ActCancel": "PUR-016", "pprf615ProtocolCancel": "PUR-016",
    "protocolCancellation": "PUR-016",
    "epClarificationDocRequest": "PUR-007", "pprf615ClarificationRequest": "PUR-007",
    "epClarificationResultRequest": "PUR-007", "epClarificationDoc": "PUR-007", "pprf615Clarification": "PUR-007",
    "epClarificationResult": "PUR-007", "explanation": "PUR-007", "explanationRequest": "PUR-007",
    "epProtocolEvasion": "PUR-013", "pprf615ActEvasion": "PUR-013", "epProtocolDeviation": "PUR-013",
    "pprf615ActDeviation": "PUR-013", "epProtocolEvDevCancel": "PUR-013",
    "fcsPurchaseDocument": "PUR-006",
    "cpContractProject": "PUR-015", "cpContractProjectChange": "PUR-015", "cpContractProjectLKP": "PUR-015",
    "cpContractProjectChangeLKP": "PUR-015", "changeRequirements": "PUR-002",
    # контракт
    "cpContractSign": "CON-011", "cpContractProjectSign": "CON-011", "cpContractSignLKP": "CON-011",
    "cpProtocolDisagreements": "CON-008", "disagreementProtocol": "CON-008", "DP_PROTZ": "CON-008",
    "cpNoticeDeviation": "CON-012", "cpNoticeEvasion": "CON-012", "cpRefusalConcludeContract": "CON-012",
    "cpProcedureCancel": "CON-012", "pprf615ContractProcedureCancel": "CON-012", "cpProcedureCancelLKP": "CON-012",
    "cpProcedureCancelFailure": "CON-008",
    "contractCancel": "CON-008", "contractCancellation": "CON-007",
    "contractProcedure": "CON-005", "pprf615ContractProcedure": "CON-005", "performanceContract": "CON-005",
    "contractProcedureCancel": "CON-008",
    "contractProcedureUnilateralRefusal": "CON-009", "contractProcedureUnilateralRefusalCancel": "CON-008",
    "claimsCorrespondenceNotice": "CON-009", "parContractProcedureUnilateralRefusal": "CON-009",
    "parContractProcedureUnilateralRefusalCancel": "CON-008", "parClaimsCorrespondenceNotice": "CON-009",
    "contractAvailableForElAct": "CON-010", "elActUnstructuredSupplierTitle": "CON-010",
    "elActUnstructuredCustomerTitle": "CON-010", "ON_NSCHFDOPPR": "CON-010", "ON_NSCHFDOPPOK": "CON-010",
    "ON_AKTREZRABZ": "CON-010", "ON_AKTREZRABP": "CON-010",
    "subcontractorInfo": "CON-008", "purchaseContract": "CON-001", "purchaseContractAccount": "RPT-001",
    # жалобы и контроль
    "complaint": "CMP-001", "closedComplaint": "CMP-001", "complaintWithdraw": "CMP-001", "complaintCancel": "CMP-001",
    "closedComplaintCancel": "CMP-001", "complaintDecision": "CMP-001", "complaintTransfer": "CMP-001",
    "closedComplaintTransfer": "CMP-001", "parElectronicComplaintAccept": "CMP-001",
    "closedParElectronicComplaintAccept": "CMP-001", "parElectronicComplaintRefusal": "CMP-001",
    "closedParElectronicComplaintRefusal": "CMP-001", "complaintVerificationPlan": "CMP-001",
    "complaintVerificationResult": "CMP-001", "tenderSuspension": "CMP-001",
    "checkPlan": "CTL-001", "eventPlan": "CTL-001", "eventPlanSuspension": "CTL-001", "unplannedCheck": "CTL-001",
    "closedUnplannedCheck": "CTL-001", "unplannedCheckCancel": "CTL-001", "closedUnplannedCheckCancel": "CTL-001",
    "unplannedCheckTenderSusp": "CTL-001", "closedUnplannedCheckTenderSusp": "CTL-001", "unplannedEvent": "CTL-001",
    "unplannedEventCancel": "CTL-001", "unplannedEventSuspension": "CTL-001", "checkResult": "CTL-001",
    "closedCheckResult": "CTL-001", "eventResult": "CTL-001", "checkResultCancel": "CTL-001",
    "closedCheckResultCancel": "CTL-001", "eventResultCancel": "CTL-001", "fcsAuditResult": "CTL-001",
    "control99UniversalExtract": "CTL-001", "control99TenderPlan2020Extract": "CTL-001",
    "control99NotificationExtract": "CTL-001", "control99BeginMessage": "CTL-001", "control99RefusalMessage": "CTL-001",
    "control99NoticeCompliance": "CTL-001", "control99ProtocolMismatch": "CTL-001",
    "control99ProtocolMismatchReductFunds": "CTL-001", "decisionSuspension": "CTL-001", "missedNotice": "CTL-001",
    "notificationIssue": "CTL-001", "planMonitoringConclusion": "CTL-001", "stopCommodity": "CTL-001",
    # реестры
    "unfairSupplier2022": "RNP-001", "unfairSupplierIKZ": "RNP-001", "unfairSupplier2022Exclude": "RNP-001",
    "dishonestSupplier": "RNP-001", "dishonestApplication": "RNP-001", "dishonestSupplierReject": "RNP-001",
    "pprf615QualifiedContractor": "RNP-001", "pprf615QualifiedContractorExclude": "RNP-001",
    "pprf615QualifiedContractorExcludeCancel": "RNP-001",
    # отчёты и планы
    "fcsCustomerReportContractExecution": "RPT-001", "fcsCustomerReportSmallScaleBusiness": "RPT-001",
    "fcsCustomerReportBigProjectMonitoring": "RPT-001", "fcsCustomerReportRusProductsPurchasesVolume": "RPT-001",
    "fcsCustomerReportSingleContractor": "RPT-001", "purchasePlan": "PLN-001", "purchasePlanProject": "PLN-001",
}

IMPORTANCE_ORDER = ["low", "medium", "high", "critical"]


def event_card(code: str) -> Dict[str, str]:
    """Карточка типа события.

    Args:
        code: Код события.

    Returns:
        dict: ``{"code", "title", "group", "importance"}`` (для неизвестного кода — ``OTH-000``).
    """
    card = EVENTS.get(code) or EVENTS["OTH-000"]
    return {"code": code if code in EVENTS else "OTH-000", **card}


def max_importance(*values: Optional[str]) -> str:
    """Наибольшая важность из переданных.

    Args:
        *values: Значения ``low`` / ``medium`` / ``high`` / ``critical`` (``None`` пропускается).

    Returns:
        str: Наибольшая важность (по умолчанию ``low``).
    """
    ranks = [IMPORTANCE_ORDER.index(v) for v in values if v in IMPORTANCE_ORDER]
    return IMPORTANCE_ORDER[max(ranks)] if ranks else "low"

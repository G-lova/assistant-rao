"""Единый каталог рисков и типов событий риск-мониторинга.

Один источник кодов для всех анализаторов: файлов (``DocumentRiskAnalyzer``), XML-событий
(``EventAnalyzer`` и детерминированные правила ``xml_events``) и, позже, комплекта закупки.
Промпты и JSON-схемы получают список кодов отсюда, поэтому код риска не может «разъехаться»
между промптом, схемой и тем, что записывается во внешнюю БД (``risk_monitoring_document_risks.rule_id``).

Коды рисков — по категориям концепции (гл. 9.5, 18.10): DOC, FIN, PROC, CTR, BEH, AI.
Коды событий — по концепции (гл. 11.4–11.6), но контрактные события имеют префикс ``CON``,
чтобы не совпадать с кодами рисков ``CTR-*`` (в концепции оба набора называются CTR).

⚠️ Коды DOC-001…DOC-010 совпадают с промптом ``risk_analysis_prompt.txt``. В старом
``doc_risks_detector.py`` те же коды означали другое (DOC-001 — «Неполнота документа»).
Строки ``document_risks``, созданные старым детектором, при переходе нужно пересоздать.
"""
from typing import Dict, Iterable, List, Optional

CATALOG_VERSION = "rm-catalog-1"

LAW_44 = "Федеральный закон № 44-ФЗ"


def _r(title: str, law: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Карточка риска каталога.

    Args:
        title: Название риска.
        law: Нормативное основание по умолчанию (если модель его не указала).

    Returns:
        dict: ``{"title", "law"}``.
    """
    return {"title": title, "law": law}


RISKS: Dict[str, Dict[str, Optional[str]]] = {
    # --- Документные (содержание документа) ---
    "DOC-001": _r("Указан конкретный товарный знак (бренд)", f"п. 1 ч. 1 ст. 33 {LAW_44}"),
    "DOC-002": _r("Указана конкретная модель или артикул", f"п. 1 ч. 1 ст. 33 {LAW_44}"),
    "DOC-003": _r("Указан конкретный производитель", f"п. 1 ч. 1 ст. 33 {LAW_44}"),
    "DOC-004": _r("Редкая или уникальная характеристика, которой соответствует узкий круг товаров", f"ч. 1 ст. 33, ч. 2 ст. 8 {LAW_44}"),
    "DOC-005": _r("Избыточная детализация требований, не вытекающая из потребности", f"ст. 33 {LAW_44}"),
    "DOC-006": _r("Иное ограничивающее требование к участнику или товару", f"ч. 2 ст. 8 {LAW_44}"),
    "DOC-007": _r("Отсутствует обязательный раздел (ответственность, сроки, оплата, приёмка, гарантии)", f"ст. 34 {LAW_44}"),
    "DOC-008": _r("Противоречие в сроках, суммах или условиях внутри документа"),
    "DOC-009": _r("Неоднозначная или оценочная формулировка"),
    "DOC-010": _r("Некорректная или неполная ссылка на нормативный документ"),
    "DOC-011": _r("Смысловое изменение в новой редакции документа"),
    "DOC-012": _r("Подозрительное изменение документа"),
    "DOC-013": _r("Неполный комплект документов", f"ст. 42 {LAW_44}"),
    # --- Финансовые ---
    "FIN-001": _r("Аномальная цена", f"ст. 22 {LAW_44}"),
    "FIN-002": _r("Демпинг", f"ст. 37 {LAW_44}"),
    "FIN-003": _r("Обеспечение заявки", f"ст. 44 {LAW_44}"),
    "FIN-004": _r("Обеспечение исполнения контракта и гарантийных обязательств", f"ст. 96 {LAW_44}"),
    "FIN-005": _r("Аванс", f"ч. 13 ст. 34, ст. 96 {LAW_44}"),
    "FIN-006": _r("Необычные условия оплаты", f"ч. 13.1 ст. 34 {LAW_44}"),
    "FIN-007": _r("Аномальное изменение финансовых условий", f"ст. 95 {LAW_44}"),
    "FIN-008": _r("Несоразмерность цены и объёма исполнения"),
    "FIN-009": _r("Несбалансированная ответственность сторон (штрафы, пени)", f"ч. 4–8 ст. 34 {LAW_44}"),
    # --- Процедурные ---
    "PROC-001": _r("Низкая конкуренция"),
    "PROC-002": _r("Аномальное изменение процедуры"),
    "PROC-003": _r("Нарушение или аномалия сроков процедуры", f"ст. 42 {LAW_44}"),
    "PROC-004": _r("Проблемы с заявками"),
    "PROC-005": _r("Закупка у единственного поставщика", f"ст. 93 {LAW_44}"),
    "PROC-006": _r("Жалобы и контроль по процедуре", f"ст. 105 {LAW_44}"),
    "PROC-007": _r("Уклонение или смена победителя", f"ст. 51 {LAW_44}"),
    "PROC-008": _r("Неоднозначные или субъективные критерии оценки", f"ст. 32 {LAW_44}"),
    # --- Контрактные ---
    "CTR-001": _r("Изменение цены контракта", f"ст. 95 {LAW_44}"),
    "CTR-002": _r("Изменение объёма и состава ТРУ", f"ст. 95 {LAW_44}"),
    "CTR-003": _r("Изменение сроков контракта", f"ст. 95 {LAW_44}"),
    "CTR-004": _r("Изменение финансовых условий контракта", f"ст. 95 {LAW_44}"),
    "CTR-005": _r("Необоснованное или несоответствующее основание изменения", f"ст. 95 {LAW_44}"),
    "CTR-006": _r("Нарушение сроков исполнения", f"ст. 94 {LAW_44}"),
    "CTR-007": _r("Неполное или ненадлежащее исполнение", f"ст. 94 {LAW_44}"),
    "CTR-008": _r("Приёмка и подтверждение исполнения", f"ст. 94 {LAW_44}"),
    "CTR-009": _r("Расторжение или односторонний отказ", f"ч. 8–26 ст. 95 {LAW_44}"),
    "CTR-010": _r("Неприменение ответственности", f"ч. 4–9 ст. 34 {LAW_44}"),
    "CTR-011": _r("Изменение гарантий, ответственности или обязательств сторон", f"ст. 95 {LAW_44}"),
    # --- Поведенческие ---
    "BEH-005": _r("Аномальное распределение результатов"),
    # --- AI (семантические) ---
    "AI-001": _r("Комплексные признаки ограничения конкуренции", f"ч. 2 ст. 8 {LAW_44}"),
    "AI-002": _r("Требования выглядят составленными под конкретного производителя", f"ст. 33 {LAW_44}"),
    "AI-003": _r("Неоднозначность или субъективность требований"),
    "AI-004": _r("Смысловое изменение версии"),
    "AI-005": _r("Необоснованное требование"),
    "AI-006": _r("Новый риск-паттерн"),
    "AI-007": _r("Семантическое противоречие"),
}

# Какие коды разрешено возвращать LLM при анализе отдельного документа (файла)
DOCUMENT_RISK_CODES: List[str] = [
    "DOC-001", "DOC-002", "DOC-003", "DOC-004", "DOC-005", "DOC-006", "DOC-007", "DOC-008", "DOC-009", "DOC-010",
    "FIN-003", "FIN-004", "FIN-005", "FIN-006", "FIN-009",
    "PROC-003", "PROC-008",
    "AI-001", "AI-002", "AI-003", "AI-005", "AI-007",
]

# Риски, которые выставляет сравнение редакций документа
VERSION_RISK_CODES: List[str] = ["DOC-011", "DOC-012", "AI-004"]

# Какие коды разрешено возвращать LLM при анализе XML-события
EVENT_RISK_CODES: List[str] = [
    "FIN-001", "FIN-002", "FIN-003", "FIN-004", "FIN-005", "FIN-006", "FIN-007", "FIN-008",
    "PROC-001", "PROC-002", "PROC-003", "PROC-004", "PROC-005", "PROC-006", "PROC-007",
    "CTR-001", "CTR-002", "CTR-003", "CTR-004", "CTR-005", "CTR-006", "CTR-007", "CTR-008", "CTR-009",
    "CTR-010", "CTR-011",
    "DOC-011", "DOC-012", "AI-004", "AI-006", "BEH-005",
]

SEVERITY_LEVEL = {"low": 0.3, "medium": 0.6, "high": 0.9}


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


def default_law(code: str) -> Optional[str]:
    """Нормативное основание по умолчанию для кода риска.

    Args:
        code: Код риска.

    Returns:
        Optional[str]: Основание или ``None``.
    """
    return (RISKS.get(code) or {}).get("law")


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

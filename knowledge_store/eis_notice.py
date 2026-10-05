"""Поля раздела 1 заключения («Наличие информации…», критерии 1.1–1.46) из XML извещения ЕИС.

Критерии раздела 1 проверяются кодом, а не LLM: в извещении ЕИС (``epNotificationEOK2020`` и
аналоги) наличие сведений видно по структуре XML. Каждый критерий описан правилом
:class:`Rule`. Правила привязаны к **номеру критерия из названия поля** (``1.18. Наличие
информации о начальной цене…``), а не к ключу ``field2_1_18``: ключи различаются между формами
и годами и берутся только из справочника форм.

Результат правила — :class:`Finding`: значение ``1`` (в наличии) с доказательством
(``путь в XML = значение``), либо ``0``/``2`` (если правило явно говорит, что отсутствие элемента
означает «отсутствует»/«не предусмотрено»), либо ``None`` — XML не позволяет решить, критерий
остаётся за LLM и экспертом.

Модуль работает только со стандартной библиотекой и не обращается к сети.
"""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

PRESENT, ABSENT, NOT_PROVIDED = 1, 0, 2

CRI = "notificationInfo/customerRequirementsInfo/customerRequirementInfo"
CC = CRI + "/contractConditionsInfo"
PLAN = CC + "/contractExecutionPaymentPlan"
OBJ = "notificationInfo/purchaseObjectsInfo/notDrugPurchaseObjectsInfo/purchaseObject"
RESP = "purchaseResponsibleInfo"
PROC = "notificationInfo/procedureInfo"
CRIT = "notificationInfo/criteriaInfo/criterionInfo"

_CRITERION_RE = re.compile(r"^\s*(\d+\.\d+)\.")


@dataclass
class Finding:
    """Результат проверки одного критерия по XML.

    Attributes:
        value: ``1`` / ``0`` / ``2`` или ``None``, если XML не даёт ответа.
        evidence: Доказательство: ``путь = значение`` (до 500 символов) или пояснение.
        paths: Пути XML, по которым принято решение.
    """

    value: Optional[int]
    evidence: str = ""
    paths: List[str] = field(default_factory=list)


@dataclass
class Rule:
    """Правило проверки критерия по XML.

    Attributes:
        criterion: Номер критерия (``1.18``).
        hint: Фрагмент названия критерия (нижний регистр); защита от сдвига нумерации в другой форме.
        paths: Пути, наличие хотя бы одного непустого значения даёт ``1``.
        absent: Что вернуть, если ни один путь не заполнен: ``0``, ``2`` или ``None`` (не решать).
        custom: Необязательная функция ``(Notice) -> Optional[Finding]`` для нетипичных правил;
            если она вернула ``None``, применяется обычная логика по ``paths``.
    """

    criterion: str
    hint: str
    paths: Sequence[str] = ()
    absent: Optional[int] = None
    custom: Optional[Callable[["Notice"], Optional[Finding]]] = None


class Notice:
    """Обёртка над XML извещения: поиск значений по путям из локальных имён тегов."""

    def __init__(self, root: ET.Element):
        """Сохраняет корневой элемент извещения (``epNotification*``)."""
        self.root = root

    @classmethod
    def from_xml(cls, data) -> "Notice":
        """Разбирает XML-извещение.

        Args:
            data: ``bytes`` или ``str`` с XML (``export`` с вложенным ``epNotification*``).

        Returns:
            Notice: Обёртка над элементом извещения.

        Raises:
            ValueError: Если XML некорректен или в нём нет элемента ``epNotification*``.
        """
        try:
            root = ET.fromstring(data if isinstance(data, bytes) else data.encode("utf-8"))
        except ET.ParseError as exc:
            raise ValueError(f"некорректный XML: {exc}") from exc
        if _local(root).startswith("epNotification"):
            return cls(root)
        for child in root:
            if _local(child).startswith("epNotification"):
                return cls(child)
        raise ValueError("в XML нет элемента epNotification*")

    def elements(self, path: str) -> List[ET.Element]:
        """Возвращает все элементы по пути из локальных имён (``a/b/c``).

        Args:
            path: Путь от элемента извещения без префиксов пространств имён.

        Returns:
            list: Найденные элементы (может быть несколько, например этапы контракта).
        """
        level = [self.root]
        for name in path.split("/"):
            level = [c for e in level for c in e if _local(c) == name]
            if not level:
                return []
        return level

    def texts(self, path: str) -> List[str]:
        """Непустые значения по пути; заглушки (только нули) отбрасываются."""
        values = []
        for el in self.elements(path):
            text = (el.text or "").strip()
            if text and not _is_placeholder(text):
                values.append(text)
        return values

    def exists(self, path: str) -> bool:
        """Есть ли элемент по пути (в том числе составной, без текста)."""
        return bool(self.elements(path))

    def flag(self, path: str) -> Optional[bool]:
        """Булев признак по пути (``true``/``false``) или ``None``, если элемента нет."""
        values = [(e.text or "").strip().lower() for e in self.elements(path)]
        if not values:
            return None
        return any(v == "true" for v in values)

    def version(self) -> int:
        """Номер версии извещения (``versionNumber``), 0 если не указан."""
        values = self.texts("versionNumber")
        return int(values[0]) if values and values[0].isdigit() else 0


def _local(el: ET.Element) -> str:
    """Локальное имя тега без пространства имён."""
    return el.tag.rsplit("}", 1)[-1]


def _is_placeholder(text: str) -> bool:
    """Значение-заглушка: только нули/точки/дефисы (например, БИК ``000000000``)."""
    return bool(re.fullmatch(r"[0\s.\-]+", text))


def _evidence(notice: Notice, path: str) -> str:
    """Формирует доказательство ``путь = значение`` для первого непустого значения пути."""
    values = notice.texts(path)
    return f"{path} = {values[0]}"[:500] if values else path


def _first_present(notice: Notice, paths: Sequence[str]) -> Optional[Finding]:
    """Находит первый путь с непустым значением и собирает :class:`Finding` со значением ``1``."""
    for path in paths:
        if notice.texts(path):
            return Finding(PRESENT, _evidence(notice, path), [path])
    return None


def _stages_exist(notice: Notice) -> bool:
    """Предусмотрены ли этапы исполнения: флаг ``isExecutionStagesTerms`` или больше одного ``stageInfo``."""
    return bool(notice.flag(CC + "/isExecutionStagesTerms")) or len(notice.elements(PLAN + "/stagesInfo/stageInfo")) > 1


def _rule_specialized_org(notice: Notice) -> Optional[Finding]:
    """1.7: специализированная организация привлечена, если роль ответственного не заказчик (``CU``)."""
    role = notice.texts(RESP + "/responsibleRole")
    if role and role[0] != "CU":
        return Finding(PRESENT, f"{RESP}/responsibleRole = {role[0]}", [RESP + "/responsibleRole"])
    return None


def _rule_stage_terms(notice: Notice) -> Optional[Finding]:
    """1.17: сроки этапов — если этапы предусмотрены, берём даты окончания этапов."""
    if not _stages_exist(notice):
        return Finding(NOT_PROVIDED, "этапы исполнения контракта не предусмотрены", [])
    path = PLAN + "/stagesInfo/stageInfo/termsInfo/notRelativeTermsInfo/endDate"
    return _first_present(notice, [path]) or _first_present(
        notice, [PLAN + "/stagesInfo/stageInfo/termsInfo/relativeTermsInfo/term"])


def _rule_stage_prices(notice: Notice) -> Optional[Finding]:
    """1.19: цены этапов — если этапы предусмотрены, берём ``financeInfo/total`` этапа."""
    if not _stages_exist(notice):
        return Finding(NOT_PROVIDED, "этапы исполнения контракта не предусмотрены", [])
    return _first_present(notice, [PLAN + "/stagesInfo/stageInfo/financeInfo/total"])


def _rule_multi_contracts(notice: Notice) -> Optional[Finding]:
    """1.39: несколько контрактов — ``notProvided=true`` означает «не предусмотрено»."""
    flag = notice.flag("notificationInfo/contractConditionsInfo/contractMultiInfo/notProvided")
    if flag:
        return Finding(NOT_PROVIDED, "contractMultiInfo/notProvided = true",
                       ["notificationInfo/contractConditionsInfo/contractMultiInfo/notProvided"])
    return None


def _rule_preference_28_29(notice: Notice) -> Optional[Finding]:
    """1.28: преимущества по ст. 28 и 29 — в названии преимущества встречаются «ст. 28» или «ст. 29»."""
    path = "notificationInfo/preferensesInfo/preferenseInfo/preferenseRequirementInfo/name"
    for text in notice.texts(path):
        if re.search(r"ст\.?\s*(28|29)\b|стать\w+\s*(28|29)\b", text):
            return Finding(PRESENT, f"{path} = {text}"[:500], [path])
    return None


def _rule_preference_30(notice: Notice) -> Optional[Finding]:
    """1.29: преимущества по ч. 3 ст. 30 — в названии встречается «ч. 3 ст. 30»."""
    path = "notificationInfo/preferensesInfo/preferenseInfo/preferenseRequirementInfo/name"
    for text in notice.texts(path):
        if re.search(r"ст\.?\s*30\b", text):
            return Finding(PRESENT, f"{path} = {text}"[:500], [path])
    return None


def _procedure_rule(path: str) -> Callable[[Notice], Optional[Finding]]:
    """Правило «порядок обеспечения»: ссылка на регламент площадки — это не порядок, решение за экспертом.

    Args:
        path: Путь к ``procedureInfo`` соответствующего обеспечения.

    Returns:
        Функция для :attr:`Rule.custom`: возвращает ``Finding(None)``, если порядок сводится к «регламенту
        электронной площадки»; иначе ``None`` (применяется обычная логика по путям).
    """
    def rule(notice: Notice) -> Optional[Finding]:
        for text in notice.texts(path):
            if re.search(r"регламент", text, re.IGNORECASE):
                return Finding(None, f"{path} = {text}"[:500] + " (ссылка на регламент — решает эксперт)", [path])
        return None
    return rule


RULES: List[Rule] = [
    Rule("1.1", "наименовании заказчика", [CRI + "/customer/fullName", RESP + "/responsibleOrgInfo/fullName"]),
    Rule("1.2", "месте нахождения", [RESP + "/responsibleOrgInfo/factAddress", RESP + "/responsibleInfo/orgFactAddress"]),
    Rule("1.3", "почтовом адресе", [RESP + "/responsibleOrgInfo/postAddress", RESP + "/responsibleInfo/orgPostAddress"]),
    Rule("1.4", "электронной почты", [RESP + "/responsibleInfo/contactEMail"]),
    Rule("1.5", "контактного телефона", [RESP + "/responsibleInfo/contactPhone"]),
    Rule("1.6", "должностном лице", [RESP + "/responsibleInfo/contactPersonInfo/lastName"]),
    Rule("1.7", "специализированной организации", absent=NOT_PROVIDED, custom=_rule_specialized_org),
    Rule("1.8", "идентификационном коде закупки", [CC + "/IKZInfo/purchaseCode"]),
    Rule("1.9", "способе определения", ["commonInfo/placingWay/name"]),
    Rule("1.10", "электронной площадки", ["commonInfo/ETP/url"]),
    Rule("1.11", "наименовании объекта закупки", ["commonInfo/purchaseObjectInfo"]),
    Rule("1.12", "ктру", [OBJ + "/KTRU/code"]),
    Rule("1.13", "количестве", [OBJ + "/quantity/value"]),
    Rule("1.14", "единице измерения", [OBJ + "/OKEI/name"]),
    Rule("1.15", "месте поставки", [CC + "/deliveryPlacesInfo/byGARInfo/deliveryPlace",
                                   CC + "/deliveryPlacesInfo/byGARInfo/GARInfo/GARAddress"]),
    Rule("1.16", "сроке исполнения контракта", [PLAN + "/contractExecutionTermsInfo/notRelativeTermsInfo/endDate",
                                              PLAN + "/contractExecutionTermsInfo/relativeTermsInfo/term"]),
    Rule("1.17", "сроках исполнения отдельных этапов", custom=_rule_stage_terms),
    Rule("1.18", "начальной (максимальной) цене", [CC + "/maxPriceInfo/maxPrice", "notificationInfo/contractConditionsInfo/maxPriceInfo/maxPrice"]),
    Rule("1.19", "цене отдельных этапов", custom=_rule_stage_prices),
    Rule("1.20", "источнике финансирования", [PLAN + "/financingSourcesInfo/financeInfo/total"]),
    Rule("1.21", "наименовании валюты", ["notificationInfo/contractConditionsInfo/maxPriceInfo/currency/name"]),
    Rule("1.22", "размере аванса", [CC + "/advancePaymentSum/sumInPercents", CC + "/advancePaymentSum/sum"], absent=NOT_PROVIDED),
    Rule("1.23", "критериях оценки", [CRIT + "/costCriterionInfo/code", CRIT + "/qualitativeCriterionInfo/code"]),
    Rule("1.24", "величинах значимости", [CRIT + "/costCriterionInfo/valueInfo/value", CRIT + "/qualitativeCriterionInfo/valueInfo/value"]),
    Rule("1.25", "частью 1 статьи 31", ["notificationInfo/requirementsInfo/requirementInfo/preferenseRequirementInfo/shortName"]),
    Rule("1.26", "частью 1.1 статьи 31", []),
    Rule("1.27", "частями 2 и 2.1", ["notificationInfo/requirementsInfo/requirementInfo/addRequirements/addRequirement/content"]),
    Rule("1.28", "статьями 28 и 29", custom=_rule_preference_28_29),
    Rule("1.29", "частью 3 статьи 30", custom=_rule_preference_30),
    Rule("1.30", "запрете или об ограничении", []),  # флаги restrictionsInfo не совпали с оценкой экспертов (50%) — решает LLM
    Rule("1.31", "размере обеспечения заявки", [CRI + "/applicationGuarantee/amount", CRI + "/applicationGuarantee/part"]),
    Rule("1.32", "порядке внесения денежных средств", [CRI + "/applicationGuarantee/procedureInfo"],
         custom=_procedure_rule(CRI + "/applicationGuarantee/procedureInfo")),
    Rule("1.33", "реквизитах счета", [CRI + "/applicationGuarantee/account/settlementAccount",
                                      CRI + "/applicationGuarantee/accountBudget/accountBudgetAdmin/bankAccount"]),
    Rule("1.34", "размере обеспечения исполнения контракта", [CRI + "/contractGuarantee/amount", CRI + "/contractGuarantee/part"]),
    Rule("1.35", "порядке предоставления обеспечения исполнения", [CRI + "/contractGuarantee/procedureInfo"],
         custom=_procedure_rule(CRI + "/contractGuarantee/procedureInfo")),
    Rule("1.36", "размере обеспечения гарантийных обязательств", [CRI + "/provisionWarranty/amount", CRI + "/provisionWarranty/part"]),
    Rule("1.37", "порядке предоставления обеспечения гарантийных", [CRI + "/provisionWarranty/procedureInfo"],
         custom=_procedure_rule(CRI + "/provisionWarranty/procedureInfo")),
    Rule("1.38", "банковском и казначейском сопровождении", []),
    Rule("1.39", "нескольких контрактов", [], custom=_rule_multi_contracts),
    Rule("1.40", "одностороннего отказа", [CC + "/isOneSideRejectionSt95"]),
    Rule("1.41", "окончания срока подачи заявок", [PROC + "/collectingInfo/endDT"]),
    Rule("1.42", "первых частей заявок", [PROC + "/firstPartsDate"]),
    Rule("1.43", "процедуры подачи предложений", [PROC + "/submissionProcedureDate"]),
    Rule("1.44", "вторых частей заявок", [PROC + "/secondPartsDate"]),
    Rule("1.45", "подведения итогов", [PROC + "/summarizingDate"]),
    Rule("1.46", "предупреждении об административной", []),
]

RULES_BY_CRITERION: Dict[str, Rule] = {r.criterion: r for r in RULES}


def criterion_number(label: str) -> Optional[str]:
    """Извлекает номер критерия из названия поля (``1.18. Наличие…`` → ``1.18``)."""
    match = _CRITERION_RE.match(label or "")
    return match.group(1) if match else None


def evaluate_rule(rule: Rule, notice: Notice) -> Finding:
    """Применяет правило к извещению.

    Args:
        rule: Правило критерия.
        notice: Разобранное извещение.

    Returns:
        Finding: ``1`` с доказательством; ``0``/``2`` по политике ``absent``; иначе ``value=None``.
    """
    if rule.custom:
        found = rule.custom(notice)
        if found is not None:
            return found
    found = _first_present(notice, rule.paths) if rule.paths else None
    if found:
        return found
    if rule.absent is not None and (rule.paths or rule.custom):
        return Finding(rule.absent, "в извещении нет: " + ", ".join(rule.paths or ["(правило)"]) [:480], list(rule.paths))
    return Finding(None, "XML не позволяет решить", [])


def evaluate_section1(notice: Notice, fields: Sequence[dict]) -> Dict[str, Finding]:
    """Проверяет поля раздела 1 формы по XML извещения.

    Args:
        notice: Разобранное извещение.
        fields: Поля формы из ``pe_form_fields`` (``field_key``, ``label``, ``value_kind``);
            обрабатываются только поля с ``value_kind == 'presence'`` и номером критерия ``1.N``,
            для которого есть правило и совпадает подсказка названия.

    Returns:
        dict: ``ключ поля → Finding`` для полей, у которых есть правило (в том числе с ``value=None``).
    """
    result: Dict[str, Finding] = {}
    for item in fields:
        if item.get("value_kind") != "presence":
            continue
        number = criterion_number(item.get("label", ""))
        rule = RULES_BY_CRITERION.get(number or "")
        if not rule or rule.hint not in (item.get("label") or "").lower():
            continue
        result[item["field_key"]] = evaluate_rule(rule, notice)
    return result


def latest_notice(xml_documents: Sequence) -> Optional[Notice]:
    """Выбирает актуальное извещение из нескольких версий (по ``versionNumber``).

    Args:
        xml_documents: Тексты/байты XML (разные версии одного извещения).

    Returns:
        Notice: Извещение с наибольшим номером версии или ``None``, если ни один XML не разобрался.
    """
    notices = []
    for data in xml_documents:
        try:
            notices.append(Notice.from_xml(data))
        except ValueError:
            continue
    return max(notices, key=lambda n: n.version(), default=None)


def evaluate_all(notice: Notice) -> Dict[str, Finding]:
    """Применяет все правила раздела 1 к извещению, не обращаясь к справочнику форм.

    Результат привязан к номеру критерия (``1.18``), поэтому его можно сохранить при загрузке
    документа и позже сопоставить с полями любой формы по названию критерия.

    Args:
        notice: Разобранное извещение.

    Returns:
        dict: ``номер критерия → Finding`` (включая ``value=None`` для нерешаемых критериев).
    """
    return {rule.criterion: evaluate_rule(rule, notice) for rule in RULES}


def findings_to_json(notice: Notice, findings: Dict[str, Finding]) -> dict:
    """Сериализует результат для хранения в ``pe_documents.extraction['eis_notice']``.

    Args:
        notice: Извещение (для номера версии).
        findings: Результат :func:`evaluate_all`.

    Returns:
        dict: ``{"version": N, "criteria": {"1.18": {"value": 1, "evidence": "..."}, ...}}``;
        нерешённые критерии не включаются.
    """
    return {
        "version": notice.version(),
        "criteria": {c: {"value": f.value, "evidence": f.evidence[:500]}
                     for c, f in findings.items() if f.value is not None},
    }


def map_to_fields(data: dict, fields: Sequence[dict]) -> Dict[str, Finding]:
    """Сопоставляет сохранённые результаты с полями формы по номеру и названию критерия.

    Args:
        data: Значение ``extraction['eis_notice']`` (см. :func:`findings_to_json`).
        fields: Поля формы (``field_key``, ``label``, ``value_kind``).

    Returns:
        dict: ``ключ поля → Finding`` только для поля типа ``presence``, у которого есть решённый
        критерий с совпавшей подсказкой названия.
    """
    out: Dict[str, Finding] = {}
    criteria = (data or {}).get("criteria", {})
    for item in fields:
        if item.get("value_kind") != "presence":
            continue
        number = criterion_number(item.get("label", ""))
        rule = RULES_BY_CRITERION.get(number or "")
        found = criteria.get(number or "")
        if not rule or not found or rule.hint not in (item.get("label") or "").lower():
            continue
        out[item["field_key"]] = Finding(found["value"], found.get("evidence", ""), [])
    return out

"""Ключи «особых» критериев в форме конкретного способа закупки.

Часть логики сводного заключения привязана к конкретным критериям: размер аванса (1.22), метод обоснования НМЦК,
проект контракта (2.5), состав заявки и т. п. В форме конкурса их ключи — ``field2_1_22``, ``field2_2_5``, ``field2_2_3``,
но в формах аукциона, котировок и единственного поставщика раздел 2 устроен иначе: там проект контракта —
``field2_2_19``, а под ``field2_2_5`` лежит «2.1.5. Сроки предоставления документов». Поэтому ключи не хардкодятся,
а определяются по **названию критерия** в справочнике форм (как и правила раздела 1, см. ``eis_notice.rule_for_label``).

Раздел 4 во всех формах одинаков (``field2_4_*``). Раздел 3 одинаков во всех формах 44-ФЗ, но в 223-ФЗ нумерация другая, поэтому его критерии тоже определяются по названию.
"""
import re
import unicodedata
from dataclasses import dataclass, fields as dataclass_fields
from typing import Dict, Optional, Sequence

# логическое имя → шаблон названия критерия (нижний регистр). Шаблон должен находить ровно одно поле формы.
LABEL_PATTERNS: Dict[str, str] = {
    "advance": r"размере аванса",
    "nmck_choice": r"выберите метод обоснования",
    "method_fit": r"соответствие (?:выбранного )?метода обоснования",
    "style": r"единому стилю",
    "composition": r"требований к содержанию,? составу заявки",
    "contract": r"проекта (?:контракта|договора) (?:действующему )?законодательств",
    "market_suppliers": r"потенциальных поставщиков, предоставивших",
    "market_terms": r"сопоставимость условиям закупки",
    # раздел 3: в 44-ФЗ это 3.2–3.8, в 223-ФЗ нумерация другая (3.5 — персональные данные, 3.7 — антимонопольное)
    "education": r"об образовании в российской федерации",
    "civil": r"гражданского законодательства",
    "budget": r"бюджетного законодательства",
    "expense": r"видам расходов бюджетов",
    "personal": r"о персональных данных",
    "antitrust": r"антимонопольного законодательства",
    "documentation": r"требованиям федерального закона № 44-фз",
}


@dataclass(frozen=True)
class FormKeys:
    """Ключи особых критериев формы (``None`` — в этой форме такого критерия нет).

    Attributes:
        advance: 1.22 «Наличие информации о размере аванса».
        nmck_choice: Выбор метода обоснования НМЦК (код 1–5).
        method_fit: «Соответствие выбранного метода обоснования цены…».
        style: «Соответствие … единому стилю и отсутствию логических ошибок».
        composition: «Требования к содержанию, составу заявки…».
        contract: «Соответствие проекта контракта действующему законодательству».
        market_suppliers: «Соответствие потенциальных поставщиков, предоставивших ценовую информацию».
        market_terms: «Сопоставимость условиям закупки коммерческих и (или) финансовых условий».
        education: «Требованиям Федерального закона об образовании» (3.2).
        civil: «Требованиям гражданского законодательства».
        budget: «Требованиям бюджетного законодательства».
        expense: «Соответствие видов работ (услуг) указанным видам расходов бюджетов» (только 44-ФЗ).
        personal: «Требованиям Федерального закона о персональных данных».
        antitrust: «Требованиям антимонопольного законодательства».
        documentation: «Соответствие документации требованиям Федерального закона № 44-ФЗ» (только 44-ФЗ).
    """

    advance: Optional[str] = None
    nmck_choice: Optional[str] = None
    method_fit: Optional[str] = None
    style: Optional[str] = None
    composition: Optional[str] = None
    contract: Optional[str] = None
    market_suppliers: Optional[str] = None
    market_terms: Optional[str] = None
    education: Optional[str] = None
    civil: Optional[str] = None
    budget: Optional[str] = None
    expense: Optional[str] = None
    personal: Optional[str] = None
    antitrust: Optional[str] = None
    documentation: Optional[str] = None

    def assessed(self) -> frozenset:
        """Ключи, которые модель оценивает по документам целиком (поиск фактов их не заполняет)."""
        return frozenset(k for k in (self.style,) if k)

    def not_applicable_allowed(self) -> frozenset:
        """Критерии соответствия, где «2» (не применимо) — осмысленный ответ эксперта."""
        return frozenset(k for k in (self.market_suppliers, self.market_terms, "field2_4_3", "field2_4_4", "field2_4_5") if k)


# Ключи формы конкурса (44fz_competition_obj6): значение по умолчанию и контрольный пример для тестов.
COMPETITION = FormKeys(advance="field2_1_22", nmck_choice="field2_2_2_0", method_fit="field2_2_2_3", style="field2_2_1_6",
                       composition="field2_2_3", contract="field2_2_5", market_suppliers="field2_2_2_5_2",
                       market_terms="field2_2_2_5_3", education="field2_3_2", civil="field2_3_3", budget="field2_3_4",
                       expense="field2_3_5", personal="field2_3_6", antitrust="field2_3_8", documentation="field2_3_7")


def resolve(fields: Sequence[dict]) -> FormKeys:
    """Определяет ключи особых критериев по названиям полей формы.

    Args:
        fields: Поля формы (``field_key``, ``label``); подходят и dict, и объекты ``FormField`` после ``asdict``.

    Returns:
        FormKeys: Для каждого критерия — ключ единственного подходящего поля, иначе ``None``
        (неоднозначное совпадение не используется: лучше не заполнять, чем записать в чужое поле). Если у поля
        с ключом конкурса нет названия вовсе (пусто или совпадает с ключом — урезанная форма в тестах), берётся ключ конкурса.
    """
    unlabeled = {f["field_key"] for f in fields if (f.get("label") or "").strip() in ("", f["field_key"])}
    found: Dict[str, Optional[str]] = {}
    for name, pattern in LABEL_PATTERNS.items():
        # раздел 1 («наличие информации…») упоминает те же законы, что и раздел 3, поэтому ищем в нём только аванс
        hits = [f["field_key"] for f in fields
                if re.search(pattern, unicodedata.normalize("NFC", f.get("label") or "").lower())
                and not f["field_key"].endswith("_text")
                and f["field_key"].startswith("field2_1_") == (name == "advance")]
        default = getattr(COMPETITION, name)
        if len(hits) == 1:
            found[name] = hits[0]
        elif not hits and default in unlabeled:
            found[name] = default          # поле без названия (урезанная форма в тестах): ключ конкурса
        else:
            found[name] = None
    return FormKeys(**found)


def names() -> Sequence[str]:
    """Логические имена полей :class:`FormKeys` (для тестов)."""
    return [f.name for f in dataclass_fields(FormKeys)]

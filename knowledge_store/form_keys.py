"""Ключи «особых» критериев в форме конкретного способа закупки.

Часть логики сводного заключения привязана к конкретным критериям: размер аванса (1.22), метод обоснования НМЦК,
проект контракта (2.5), состав заявки и т. п. В форме конкурса их ключи — ``field2_1_22``, ``field2_2_5``, ``field2_2_3``,
но в формах аукциона, котировок и единственного поставщика раздел 2 устроен иначе: там проект контракта —
``field2_2_19``, а под ``field2_2_5`` лежит «2.1.5. Сроки предоставления документов». Поэтому ключи не хардкодятся,
а определяются по **названию критерия** в справочнике форм (как и правила раздела 1, см. ``eis_notice.rule_for_label``).

Разделы 3 и 4 во всех формах 44-ФЗ имеют одинаковые ключи (``field2_3_*``, ``field2_4_*``) и здесь не разбираются.
"""
import re
from dataclasses import dataclass, fields as dataclass_fields
from typing import Dict, Optional, Sequence

# логическое имя → шаблон названия критерия (нижний регистр). Шаблон должен находить ровно одно поле формы.
LABEL_PATTERNS: Dict[str, str] = {
    "advance": r"размере аванса",
    "nmck_choice": r"выберите метод обоснования",
    "method_fit": r"соответствие (?:выбранного )?метода обоснования",
    "style": r"единому стилю",
    "composition": r"требований к содержанию,? составу заявки",
    "contract": r"проекта контракта действующему",
    "market_suppliers": r"потенциальных поставщиков, предоставивших",
    "market_terms": r"сопоставимость условиям закупки",
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
    """

    advance: Optional[str] = None
    nmck_choice: Optional[str] = None
    method_fit: Optional[str] = None
    style: Optional[str] = None
    composition: Optional[str] = None
    contract: Optional[str] = None
    market_suppliers: Optional[str] = None
    market_terms: Optional[str] = None

    def assessed(self) -> frozenset:
        """Ключи, которые модель оценивает по документам целиком (поиск фактов их не заполняет)."""
        return frozenset(k for k in (self.style,) if k)

    def not_applicable_allowed(self) -> frozenset:
        """Критерии соответствия, где «2» (не применимо) — осмысленный ответ эксперта."""
        return frozenset(k for k in (self.market_suppliers, self.market_terms, "field2_4_3", "field2_4_4", "field2_4_5") if k)


# Ключи формы конкурса (44fz_competition_obj6): значение по умолчанию и контрольный пример для тестов.
COMPETITION = FormKeys(advance="field2_1_22", nmck_choice="field2_2_2_0", method_fit="field2_2_2_3", style="field2_2_1_6",
                       composition="field2_2_3", contract="field2_2_5", market_suppliers="field2_2_2_5_2",
                       market_terms="field2_2_2_5_3")


def resolve(fields: Sequence[dict]) -> FormKeys:
    """Определяет ключи особых критериев по названиям полей формы.

    Args:
        fields: Поля формы (``field_key``, ``label``); подходят и dict, и объекты ``FormField`` после ``asdict``.

    Returns:
        FormKeys: Для каждого критерия — ключ единственного подходящего поля, иначе ``None``
        (неоднозначное совпадение не используется: лучше не заполнять, чем записать в чужое поле). Если у поля
        с ключом конкурса нет названия вовсе, берётся ключ конкурса.
    """
    unlabeled = {f["field_key"] for f in fields if not (f.get("label") or "").strip()}
    found: Dict[str, Optional[str]] = {}
    for name, pattern in LABEL_PATTERNS.items():
        hits = [f["field_key"] for f in fields
                if re.search(pattern, (f.get("label") or "").lower()) and not f["field_key"].endswith("_text")]
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

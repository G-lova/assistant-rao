"""Проверка экспертного заключения первичного эксперта (детерминированный слой, без модели и сети).

Сводный эксперт ставит первичному эксперту оценку ``range`` (1.00 / 0.75 / 0.50 / 0.00) и пишет комментарий.
Модуль находит в ``data`` заключения объективные дефекты — их можно назвать в комментарии без оговорок:

* пустой или заглушечный перечень документов, шаблонные тексты («тест», «____»);
* есть «0», а замечания (``field3``) пусты; замечания есть, а «0» нет;
* заключение (``field4``) пустое или осталось шаблоном;
* для отчётных документов (объект 7) — не заполнен перечень позиций, позиция без названия.

Результат — список :class:`Issue` с тяжестью (``minor`` / ``major``). Оценку ``range`` по ним предсказывать нельзя:
на истории (6,5 тыс. оценённых заключений) эти дефекты почти не связаны с оценкой, она сильнее зависит от того,
кто её ставит. Модуль даёт материал для комментария, а не оценку. Сравнение с заключением сводного эксперта — отдельный слой.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MINOR = "minor"
MAJOR = "major"

# Листовой критерий формы: field2_1_7, field2_2_1_6, field2_3_5 …; суффиксы _text/_unit — не критерии
_CRITERION_RE = re.compile(r"^field2_\d+(?:_\d+)+$")
# Выбор метода НМЦК хранит номер метода 1–5, а не «наличие» — в проверках согласованности не участвует
_NON_BOOLEAN_KEYS = {"field2_2_2_0"}
# Заглушки в текстовых полях
_JUNK_RE = re.compile(r"(?:^|[\s.,;:])(?:тест\w*|test|asdf\w*|йцук\w*|ыва|аааа+|xxx+)(?=$|[\s.,;:])|_{3,}|\blorem\b", re.I)
# Начало шаблона блока замечаний, которое эксперт мог оставить без содержания
_TEMPLATE_HEAD_RE = re.compile(r"^\s*\d+\.\s*Комментарий к разделу[^\n]*\n?", re.I)
MIN_DOCS_LEN = 15
MIN_REMARKS_LEN = 30


@dataclass
class Issue:
    """Объективный дефект заключения.

    Attributes:
        code: Код проверки (``ZERO_NO_TEXT`` …).
        severity: ``minor`` или ``major``.
        keys: Поля формы, к которым относится замечание (до 10).
        message: Формулировка, которую можно привести в комментарии сводного эксперта.
    """

    code: str
    severity: str
    message: str
    keys: List[str] = field(default_factory=list)


def normalize(value: Any) -> Optional[int]:
    """Приводит значение критерия к ``1`` / ``0`` / ``2`` либо ``None`` (пусто, не число и не булево)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value) if value in (0, 1, 2) else None
    text = str(value).strip().lower() if value is not None else ""
    return {"true": 1, "false": 0, "1": 1, "0": 0, "2": 2}.get(text)


def _text(value: Any) -> str:
    """Строка из значения поля (``None`` и нестроки → пустая строка)."""
    return value.strip() if isinstance(value, str) else ""


def is_report_data(data: Dict[str, Any]) -> bool:
    """Заключение по отчётным документам (объект 7): ``field1_3`` — список элементов, а не строка способа финансирования."""
    return isinstance(data.get("field1_3"), list)


def criteria(data: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Листовые критерии заключения 44-ФЗ/223-ФЗ: ``ключ → 1/0/2/None``.

    Родительские итоги (у которых есть вложенные ключи) и поля вне ``_CRITERION_RE`` не включаются.
    """
    keys = [k for k in data if _CRITERION_RE.match(k)]
    parents = {k.rsplit("_", 1)[0] for k in keys}
    return {k: normalize(data[k]) for k in keys if k not in parents and k not in _NON_BOOLEAN_KEYS}


def _check_texts(data: Dict[str, Any], out: List[Issue]) -> None:
    """Пустой перечень документов, заглушки в текстовых полях."""
    docs = _text(data.get("documents") if "documents" in data else data.get("field1_1"))
    if len(docs) < MIN_DOCS_LEN:
        out.append(Issue("EMPTY_DOCS", MAJOR, "не заполнен перечень документов, поступивших на экспертизу",
                         ["documents" if "documents" in data else "field1_1"]))
    junk = [k for k, v in data.items() if isinstance(v, str) and _JUNK_RE.search(v) and k not in ("field4",)]
    if junk:
        out.append(Issue("JUNK_TEXT", MINOR, "в текстовых полях остались заглушки («тест», «____»)", junk[:10]))


def _count_zeros(data: Dict[str, Any]) -> int:
    """Число листовых критериев со значением «0» (нужно для проверки блока замечаний).

    Проверки «нет пояснения к нулю», «итог подраздела не равен критериям» и «неотвеченные критерии» исключены:
    на 6,5 тыс. оценённых заключений они срабатывают у 60–85% заключений при любой оценке ``range`` —
    эксперты так форму не заполняют (пояснение есть у половины нулей, итоги подразделов не агрегаты).
    """
    return sum(1 for v in criteria(data).values() if v == 0)


def _check_remarks(data: Dict[str, Any], zeros: int, out: List[Issue]) -> None:
    """Блок замечаний (``field3``) и заключение (``field4``)."""
    remarks = _TEMPLATE_HEAD_RE.sub("", _text(data.get("field3")))
    if zeros and len(remarks) < MIN_REMARKS_LEN:
        out.append(Issue("ZERO_NO_REMARKS", MAJOR, "есть критерии с отрицательной оценкой, но блок замечаний не заполнен", ["field3"]))
    if not zeros and re.search(r"\b\d\.\d", remarks) and len(remarks) >= MIN_REMARKS_LEN:
        out.append(Issue("REMARKS_NO_ZERO", MINOR, "замечания указаны, а отрицательных оценок критериев нет", ["field3"]))
    conclusion = _text(data.get("field4"))
    if "field4" in data and (len(conclusion) < MIN_REMARKS_LEN or "__" in conclusion):
        out.append(Issue("CONCLUSION_TEMPLATE", MAJOR, "заключение пустое или содержит незаполненные места шаблона", ["field4"]))


def _check_report(data: Dict[str, Any], out: List[Issue]) -> None:
    """Проверки заключения по отчётным документам: элементы ``field1_3`` и их отметки о недостатках."""
    items = [i for i in data.get("field1_3") or [] if isinstance(i, dict)]
    if not items:
        out.append(Issue("NO_ITEMS", MAJOR, "не заполнен перечень проверяемых позиций (товары/работы/услуги)", ["field1_3"]))
        return
    if any(not _text(i.get("name")) for i in items):
        out.append(Issue("ITEM_NO_NAME", MAJOR, "у позиции не указано название", ["field1_3[].name"]))
    junk = [k for i in items for k, v in i.items() if isinstance(v, dict) and _JUNK_RE.search(_text(v.get("q2")))]
    if junk:
        out.append(Issue("JUNK_TEXT", MINOR, "в описании недостатков остались заглушки", sorted(set(junk))[:10]))


def compare_method(primary: Dict[str, Any], summary: Dict[str, Any]) -> Optional[Issue]:
    """Методологическая ошибка: метод обоснования НМЦК (``field2_2_2_0``) первичного отличается от итогового.

    Поле заполняется редко (в истории ~2% заключений), поэтому при пустом значении у любого из заключений проверка
    не срабатывает. Только для закупок.

    Returns:
        Issue | None: ``METHOD_MISMATCH`` или ``None``.
    """
    if is_report_data(summary):
        return None
    # метод может храниться числом или строкой («1» / 1): сравниваем как строки
    ours, theirs = (str(v).strip() if v is not None else "" for v in (primary.get("field2_2_2_0"), summary.get("field2_2_2_0")))
    if not ours or not theirs or ours == theirs:
        return None
    return Issue("METHOD_MISMATCH", MAJOR, "метод обоснования НМЦК выбран иначе, чем в итоговом заключении", ["field2_2_2_0"])


def review_opinion(data: Dict[str, Any]) -> List[Issue]:
    """Находит объективные дефекты заключения первичного эксперта.

    Args:
        data: ``expertise_expert_opinion7s.data`` (``dict``).

    Returns:
        list[Issue]: Дефекты; пустой список — замечаний нет.
    """
    out: List[Issue] = []
    _check_texts(data, out)
    if is_report_data(data):
        _check_report(data, out)
    else:
        _check_remarks(data, _count_zeros(data), out)
    return out

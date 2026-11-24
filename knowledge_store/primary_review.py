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

try:
    from configs import settings
except ImportError:   # модуль работает и без настроек (тесты, скрипты)
    settings = None

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


def _number(value: Any) -> Optional[float]:
    """Число из значения поля (``1 288,97`` → 1288.97); пусто и не число → ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[\s\u00a0]", "", str(value or "")).replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _first(data: Dict[str, Any], keys) -> Any:
    """Значение первого заполненного ключа из списка (товары и работы хранят одно и то же под разными ключами)."""
    return next((data[k] for k in keys if data.get(k) not in (None, "")), None)


def compare_report_facts(primary: Dict[str, Any], summary: Dict[str, Any],
                         labels: Optional[Dict[str, str]] = None) -> List[Issue]:
    """Сверяет с итоговым заключением не-критериальные поля отчётных документов (объект 7).

    * **Анкетные факты** (``PRIMARY_REVIEW_FACT_FIELDS``: требование субподряда из СМП, СМП-исполнитель в работах/НИР,
      требуется ли расчёт неустойки): ответ — факт, а не оценка, поэтому сравнивается на равенство; расхождение —
      ``FACT_MISMATCH``. Зависимые вопросы сравниваются, только если родитель «да» у обоих заключений
      (``PRIMARY_REVIEW_FACT_PARENT``), поля одной формы — при наличии маркера формы (``PRIMARY_REVIEW_FACT_REQUIRES``).
    * **Суммы неустойки** (``PRIMARY_REVIEW_PENALTY_FIELDS``: пени, штрафы, суммы заказчика): расхождение больше допуска
      ``PRIMARY_REVIEW_PENALTY_TOLERANCE`` — ``PENALTY_MISMATCH``. Если по одному из заключений расчёт неустойки не требуется
      (``field*_1_11`` = «нет»), суммы не сверяются — расхождение уже названо в ``FACT_MISMATCH``. Пустая сумма = 0.

    В оценку (``range``) результат не входит — только в комментарий и ``issues``.

    Args:
        primary: ``data`` заключения первичного эксперта.
        summary: ``data`` итогового заключения.
        labels: ``ключ → название поля`` формы (необязательно); без названия берётся из настроек.

    Returns:
        list[Issue]: Расхождения; для закупок и при пустых полях — пустой список.
    """
    if not (is_report_data(primary) and is_report_data(summary)):
        return []
    labels = labels or {}
    facts = getattr(settings, "PRIMARY_REVIEW_FACT_FIELDS", {}) if settings else {}
    penalty = getattr(settings, "PRIMARY_REVIEW_PENALTY_FIELDS", {}) if settings else {}
    tolerance = float(getattr(settings, "PRIMARY_REVIEW_PENALTY_TOLERANCE", 1.0)) if settings else 1.0
    out: List[Issue] = []
    answer = {1: "«да»", 0: "«нет»"}
    requires = getattr(settings, "PRIMARY_REVIEW_FACT_REQUIRES", {}) if settings else {}
    parents = getattr(settings, "PRIMARY_REVIEW_FACT_PARENT", {}) if settings else {}
    for key, default in facts.items():
        marker = requires.get(key)           # поле одной формы: в другой тот же ключ означает иное (field5_0_1)
        if marker and not (marker in primary and marker in summary):
            continue
        parent = parents.get(key)            # зависимый вопрос показывается, только если родитель «да» у обоих
        if parent and not (normalize(primary.get(parent)) == 1 and normalize(summary.get(parent)) == 1):
            continue
        ours, theirs = normalize(primary.get(key)), normalize(summary.get(key))
        if ours is None or theirs is None or ours == theirs or ours not in answer or theirs not in answer:
            continue
        name = re.sub(r"\s+", " ", labels.get(key) or default).strip().rstrip("?:")
        out.append(Issue("FACT_MISMATCH", MAJOR, f"неверно указан ответ на вопрос «{name}»: у вас {answer[ours]}, в итоговом заключении {answer[theirs]}", [key]))
    required = [normalize(_first(d, ("field5_1_11", "field6_1_11"))) for d in (primary, summary)]
    if 0 in required:         # расчёт неустойки не требуется хотя бы по одному заключению: суммы не сравниваем
        return out
    for name, keys in penalty.items():
        raw_ours, raw_theirs = _number(_first(primary, keys)), _number(_first(summary, keys))
        ours, theirs = raw_ours or 0.0, raw_theirs or 0.0
        if abs(ours - theirs) > tolerance:
            # «рублей» без точки: build_comment снимает точку в конце замечания, «руб» без точки выглядит небрежно
            mine = "не заполнено" if raw_ours is None else f"{ours:.2f} рублей"
            out.append(Issue("PENALTY_MISMATCH", MAJOR,
                             f"неверная сумма в расчёте неустойки ({name}): у вас {mine}, в итоговом заключении {theirs:.2f} рублей",
                             [k for k in keys if k in primary or k in summary][:2]))
    return out


def issue_penalty(grade: Optional[float], issues: List[Issue], percent: Optional[float] = None,
                  spec: Optional[dict] = None) -> tuple:
    """Снижает предлагаемую оценку за серьёзные замечания к заключению первичного эксперта.

    Три ограничения, чтобы штраф не был грубым:

    1. влияют только серьёзные замечания (``MAJOR``); мелкие (в том числе замечания модели к тексту) идут лишь в комментарий;
    2. замечание — вычет в процентных пунктах из взвешенного процента сходства (``PRIMARY_REVIEW_ISSUE_PERCENT`` по коду,
       иначе ``PRIMARY_REVIEW_ISSUE_PERCENT_DEFAULT``); замечания одного кода считаются один раз; сумма вычетов не больше
       ``PRIMARY_REVIEW_ISSUE_MAX_PERCENT``;
    3. оценка меняется, только если процент после вычета ушёл ниже порога текущей ступени, то есть замечание «добивает»
       пограничную оценку; больше чем на одну ступень оценка не снижается.

    Args:
        grade: Оценка до штрафа (``None`` — оценки нет, штраф не применяется).
        issues: Замечания к заключению (``review_opinion``, ``compare_*``, замечания модели).
        percent: Взвешенный процент сходства (``None`` — процента нет, штраф не применяется).
        spec: Параметры группы из :func:`primary_score.load_model` (нужны пороги).

    Returns:
        tuple: ``(оценка после штрафа, вычет в п. п., число снятых ступеней 0/1)``.
    """
    from knowledge_store import primary_score

    if grade is None or percent is None or not spec or not settings:
        return grade, 0.0, 0
    by_code = getattr(settings, "PRIMARY_REVIEW_ISSUE_PERCENT", {}) or {}
    default = float(getattr(settings, "PRIMARY_REVIEW_ISSUE_PERCENT_DEFAULT", 0.0))
    codes = {i.code for i in issues if i.severity == MAJOR}                   # (1) только серьёзные, (2) код — один раз
    deduction = min(sum(float(by_code.get(code, default)) for code in codes),
                    float(getattr(settings, "PRIMARY_REVIEW_ISSUE_MAX_PERCENT", 5.0)))
    if deduction <= 0:
        return grade, 0.0, 0
    lowered = primary_score.grade_by_percent(percent - deduction, spec)       # (3) оценка по порогам после вычета
    grades = primary_score.GRADES
    index = min(range(len(grades)), key=lambda k: abs(grades[k] - grade))
    new_grade = max(lowered, grades[max(0, index - 1)])                       # не ниже чем на одну ступень
    new_grade = min(new_grade, grade)                                         # штраф оценку не повышает
    return new_grade, round(deduction, 1), int(new_grade < grade)


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

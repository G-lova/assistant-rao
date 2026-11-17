"""Сравнение заключения первичного эксперта с итоговым заключением сводного эксперта.

Оценка ``range`` ставится после готового сводного заключения, поэтому его значения — эталон. Модуль:

* находит расхождения по листовым критериям (:func:`compare`);
* собирает комментарий сводного эксперта из расхождений (:func:`build_comment`) — только по фактам сравнения;
* предлагает оценку по числу расхождений (:func:`suggest_range`; пороги — ``configs.settings.PRIMARY_REVIEW_THRESHOLDS``).

Ограничения (по истории 3,5 тыс. оценок): число расхождений слабо связано с оценкой (корреляция около −0.23,
точное совпадение ~44% при базе 33–36%; пороги подобраны на тех же данных, то есть оценка оптимистична), потому что оценка зависит и от того, кто её ставит, и от текстовых полей,
которые здесь не сравниваются. Поэтому оценка — ориентир, а комментарий — основной результат.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from knowledge_store import primary_review
from knowledge_store.primary_review import normalize

try:
    from configs import settings
except ImportError:   # модуль работает и без настроек (тесты, скрипты)
    settings = None

DEFAULT_THRESHOLDS = {"procurement": (7, 10, 22), "report": (2, 3, 10)}
DEFAULT_MIN_COMMON = 5
# Итоговые флаги отчётных документов (объект 7): field3_0, field4_2 …; подкритерии field3_0_3 — тоже
_REPORT_KEY_RE = re.compile(r"^field[345]_\d+(?:_\d+)?$")
WORDS = {1: "в наличии / соответствует", 0: "отсутствует / не соответствует", 2: "не предусмотрено / не применимо"}
POSITIVE_COMMENT = "ЭЗ соответствует требованиям"


@dataclass
class Difference:
    """Расхождение по одному критерию.

    Attributes:
        key: Ключ поля формы.
        primary: Значение первичного эксперта (1/0/2).
        summary: Значение в итоговом заключении.
    """

    key: str
    primary: int
    summary: int

    @property
    def kind(self) -> str:
        """``missed`` — первичный не увидел нарушение; ``false_alarm`` — увидел то, чего нет; ``other`` — 1 против 2."""
        if self.summary == 0:
            return "missed"
        if self.primary == 0:
            return "false_alarm"
        return "other"


@dataclass
class Comparison:
    """Результат сравнения двух заключений.

    Attributes:
        common: Сколько критериев решено в обоих заключениях.
        differences: Расхождения, «пропущенные нарушения» первыми.
    """

    common: int = 0
    differences: List[Difference] = field(default_factory=list)
    group: str = "procurement"      # ``procurement`` — закупка, ``report`` — отчётные документы (объект 7)

    def count(self, kind: Optional[str] = None) -> int:
        """Число расхождений (всех или заданного вида)."""
        return sum(1 for d in self.differences if kind is None or d.kind == kind)


def comparable(data: Dict[str, Any]) -> Dict[str, int]:
    """Критерии заключения, пригодные для сравнения: ``ключ → 1/0/2`` (пустые и нечисловые пропускаются).

    Для закупок это листовые критерии раздела 2; для отчётных документов — булевы флаги ``field3_*``/``field4_*``/``field5_*``.
    """
    if primary_review.is_report_data(data):
        raw = {k: v for k, v in data.items() if _REPORT_KEY_RE.match(k) and isinstance(v, (bool, int))}
    else:
        raw = primary_review.criteria(data)
    return {k: n for k, v in raw.items() if (n := normalize(v)) is not None}


_ORDER = {"missed": 0, "false_alarm": 1, "other": 2}


def compare(primary: Dict[str, Any], summary: Dict[str, Any]) -> Comparison:
    """Сравнивает критерии первичного заключения с итоговым.

    Args:
        primary: ``data`` заключения первичного эксперта.
        summary: ``data`` итогового сводного заключения.

    Returns:
        Comparison: Число общих критериев и расхождения, отсортированные: пропуск нарушения → ложное нарушение → прочее.
    """
    a, b = comparable(primary), comparable(summary)
    common = sorted(set(a) & set(b), key=_key_order)
    diffs = [Difference(k, a[k], b[k]) for k in common if a[k] != b[k]]
    diffs.sort(key=lambda d: (_ORDER[d.kind], _key_order(d.key)))
    return Comparison(len(common), diffs, "report" if primary_review.is_report_data(primary) else "procurement")


def _key_order(key: str) -> List[int]:
    """Ключ сортировки: ``field2_2_10`` после ``field2_2_9``."""
    return [int(p) for p in re.findall(r"\d+", key)]


def suggest_range(cmp: Comparison, thresholds: Optional[Sequence[int]] = None,
                  min_common: Optional[int] = None) -> Optional[float]:
    """Предлагает оценку по числу расхождений.

    Args:
        cmp: Результат :func:`compare`.
        thresholds: Три порога числа расхождений (для 1.00 / 0.75 / 0.50); по умолчанию — из настроек для группы случая.
        min_common: Минимум общих критериев; при меньшем числе оценка не предлагается.

    Returns:
        float | None: 1.00 / 0.75 / 0.50 / 0.00; ``None`` — слишком мало общих критериев.
    """
    table = getattr(settings, "PRIMARY_REVIEW_THRESHOLDS", DEFAULT_THRESHOLDS)
    t = tuple(thresholds or table[cmp.group])
    floor = min_common if min_common is not None else getattr(settings, "PRIMARY_REVIEW_MIN_COMMON", DEFAULT_MIN_COMMON)
    if cmp.common < floor:
        return None
    n = cmp.count()
    return 1.0 if n < t[0] else 0.75 if n < t[1] else 0.5 if n < t[2] else 0.0


def build_comment(cmp: Comparison, labels: Optional[Dict[str, str]] = None,
                  issues: Optional[Sequence[primary_review.Issue]] = None, limit: int = 15) -> str:
    """Собирает комментарий сводного эксперта из расхождений и объективных дефектов.

    Args:
        cmp: Результат :func:`compare`.
        labels: ``ключ поля → название критерия`` (из ``pe_form_fields``); без названия выводится ключ.
        issues: Дефекты из :func:`knowledge_store.primary_review.review_opinion` (необязательно).
        limit: Максимум пунктов о расхождениях; остальные сводятся в одну строку.

    Returns:
        str: Нумерованный список замечаний; при полном совпадении и отсутствии дефектов — ``POSITIVE_COMMENT``.
    """
    labels = labels or {}
    lines: List[str] = []
    titles = {"missed": "не выявлено нарушение, которое есть в итоговом заключении",
              "false_alarm": "указано нарушение, которого нет в итоговом заключении",
              "other": "значение не совпадает с итоговым заключением"}
    for d in cmp.differences[:limit]:
        label = labels.get(d.key) or d.key
        lines.append(f"{label}: {titles[d.kind]} (эксперт — «{WORDS[d.primary]}», итог — «{WORDS[d.summary]}»).")
    if len(cmp.differences) > limit:
        lines.append(f"Ещё расхождений: {len(cmp.differences) - limit}.")
    lines += [i.message[0].upper() + i.message[1:] + "." for i in issues or []]
    if not lines:
        return POSITIVE_COMMENT
    return "\n".join(f"{n}. {text}" for n, text in enumerate(lines, start=1))

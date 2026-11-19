"""Сравнение заключения первичного эксперта с итоговым заключением сводного эксперта.

Оценка ``range`` ставится после готового сводного заключения, поэтому его значения — эталон. По «Порядку включения/
исключения в Реестр» (2025) член Экспертной группы анализирует каждое заключение эксперта на соответствие разделам
сводного заключения: полное соответствие — 1 балл, незначительные (по его мнению) отклонения — 0,75, существенные
расхождения — 0,50. Критерия «незначительно / существенно» порядок не задаёт, поэтому оценка субъективна; 0 в порядке
не описан и на истории почти всегда сопровождает отказ в принятии заключения (``accept`` = 0 в 345 из 351 случаев).
Проверка признаков по разделам (``field2_S_*``) качество модели не улучшила (58,2% против 58,1%), в модель не вошла.
Модуль:

* находит расхождения по листовым критериям (:func:`compare`);
* собирает комментарий сводного эксперта из расхождений (:func:`build_comment`) — только по фактам сравнения;
* предлагает оценку по числу расхождений (:func:`suggest_range`; пороги — ``configs.settings.PRIMARY_REVIEW_THRESHOLDS``).

Ограничения (по истории 3,5 тыс. оценок): число расхождений слабо связано с оценкой (корреляция около −0.23,
точное совпадение ~44% при базе 33–36%; пороги подобраны на тех же данных, то есть оценка оптимистична), потому что оценка зависит и от того, кто её ставит, и от текстовых полей,
которые здесь не сравниваются. Поэтому оценка — ориентир, а комментарий — основной результат.
"""
import difflib
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from knowledge_store import primary_review, primary_taxonomy
from knowledge_store.primary_review import normalize

try:
    from configs import settings
except ImportError:   # модуль работает и без настроек (тесты, скрипты)
    settings = None

DEFAULT_THRESHOLDS = {"procurement": (13, 21, 39), "report": (2, 3, 10)}
DEFAULT_SCALE = ((25, 0.0), (50, 0.5), (75, 0.75))
DEFAULT_WEIGHTS = {"procurement": (3, 1, 1), "report": (1, 1, 1)}   # (пропуск нарушения, ложное нарушение, прочее)
DEFAULT_MIN_COMMON = 5
# Итоговые флаги отчётных документов (объект 7): field3_0, field4_2 …; подкритерии field3_0_3 — тоже
_REPORT_KEY_RE = re.compile(r"^field[345]_\d+(?:_\d+)?$")
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

    @property
    def match_percent(self) -> Optional[float]:
        """Доля общих критериев, по которым первичный эксперт совпал с итоговым заключением, в процентах."""
        return None if not self.common else round(100 * (1 - len(self.differences) / self.common), 1)

    def score(self, weights: Optional[Sequence[float]] = None) -> float:
        """Взвешенное число расхождений: пропущенное нарушение тяжелее остальных (веса — из настроек для группы)."""
        w = tuple(weights or getattr(settings, "PRIMARY_REVIEW_WEIGHTS", DEFAULT_WEIGHTS)[self.group])
        return w[0] * self.count("missed") + w[1] * self.count("false_alarm") + w[2] * self.count("other")


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
        thresholds: Три порога взвешенного числа расхождений (для 1.00 / 0.75 / 0.50); по умолчанию — из настроек для группы случая.
        min_common: Минимум общих критериев; при меньшем числе оценка не предлагается.

    Returns:
        float | None: 1.00 / 0.75 / 0.50 / 0.00; ``None`` — слишком мало общих критериев.
    """
    table = getattr(settings, "PRIMARY_REVIEW_THRESHOLDS", DEFAULT_THRESHOLDS)
    t = tuple(thresholds or table[cmp.group])
    floor = min_common if min_common is not None else getattr(settings, "PRIMARY_REVIEW_MIN_COMMON", DEFAULT_MIN_COMMON)
    if cmp.common < floor:
        return None
    n = cmp.score()
    return 1.0 if n < t[0] else 0.75 if n < t[1] else 0.5 if n < t[2] else 0.0


def criterion_number(key: str, labels: Dict[str, str]) -> str:
    """Номер критерия для комментария: из названия (``1.30. Наличие…`` → ``1.30``), иначе ключ поля."""
    match = re.match(r"\s*(\d+(?:\.\d+)+)\.?\s", labels.get(key) or "")
    return match.group(1) if match else key


def apply_full_match_rule(grade: Optional[float], cmp: Comparison, shared: Optional[Set[str]] = None,
                          mode: Optional[str] = "settings", percent: Optional[float] = None) -> Tuple[Optional[float], bool]:
    """Постпроверка оценки по порядку включения в Реестр: 1 балл — только при полном соответствии сводному заключению.

    Модель обучена на поведении оценивающих, которые на истории ставят 1.00 и при наличии отклонений (67% случаев без
    пропусков нарушений), поэтому оценку 1.00 она выдаёт чаще, чем допускает порядок. Постпроверка снижает 1.00 до 0.75,
    если остались расхождения, которые не общие для всех первичных экспертов (:func:`shared_keys`).

    Args:
        grade: Оценка модели или порогов.
        cmp: Результат :func:`compare`.
        shared: Расхождения, общие для всех первичных (не засчитываются).
        mode: ``any`` — любое расхождение; ``missfa`` — только пропуск нарушения и лишнее нарушение (1 против 2 не в счёт);
            ``missed`` — только пропуск нарушения; ``None`` — без постпроверки; ``settings`` (по умолчанию) — режим из
            ``settings.PRIMARY_REVIEW_FULL_MATCH_RULE``.
        percent: Взвешенный процент сходства для исключения (по умолчанию — ``cmp.match_percent``).

    Исключение: если процент сходства выше ``PRIMARY_REVIEW_FULL_MATCH_EXEMPT_PERCENT``, оценка не снижается. Процент —
    взвешенный (``percent``, :mod:`knowledge_store.primary_score`), без него — обычный ``match_percent``.

    Returns:
        tuple: ``(оценка, изменена_ли)``.
    """
    if mode == "settings":
        mode = getattr(settings, "PRIMARY_REVIEW_FULL_MATCH_RULE", "any")
    if grade is None or grade < 1.0 or mode is None:
        return grade, False
    exempt = getattr(settings, "PRIMARY_REVIEW_FULL_MATCH_EXEMPT_PERCENT", None)
    similarity = percent if percent is not None else cmp.match_percent
    if exempt is not None and similarity is not None and similarity > exempt:
        return grade, False        # почти полное совпадение: единичные расхождения 1.00 не отменяют
    kinds = {"any": ("missed", "false_alarm", "other"), "missfa": ("missed", "false_alarm"), "missed": ("missed",)}[mode]
    if any(d.kind in kinds and d.key not in (shared or ()) for d in cmp.differences):
        return 0.75, True
    return grade, False


def range_by_scale(percent: Optional[float]) -> Optional[float]:
    """Оценка по шкале аналитика (``PRIMARY_REVIEW_SCALE``): 0–25% → 0, 25–50% → 0.5, 50–75% → 0.75, выше 75% → 1.

    Args:
        percent: Процент, например :attr:`Comparison.match_percent`; ``None`` — оценка не предлагается.

    Returns:
        float | None: 0 / 0.5 / 0.75 / 1.
    """
    if percent is None:
        return None
    for upper, value in getattr(settings, "PRIMARY_REVIEW_SCALE", DEFAULT_SCALE):
        if percent <= upper:
            return value
    return getattr(settings, "PRIMARY_REVIEW_SCALE_TOP", 1.0)


def shared_keys(comparisons: Sequence[Comparison]) -> Set[str]:
    """Критерии, по которым все первичные эксперты разошлись с итоговым заключением одинаково.

    Если два и более первичных эксперта независимо дали одно значение, а итоговое заключение иное, вероятнее всего
    итоговое значение выбрано сводным экспертом сознательно (или требует проверки), а не ошибка первичных.

    Args:
        comparisons: Результаты :func:`compare` по всем первичным экспертам экспертизы.

    Returns:
        set[str]: Ключи полей; пусто, если первичных меньше двух.
    """
    if len(comparisons) < 2:
        return set()
    per_expert = [{d.key: (d.primary, d.summary) for d in c.differences} for c in comparisons]
    first = per_expert[0]
    return {k for k, pair in first.items() if all(e.get(k) == pair for e in per_expert[1:])}


def build_comment(cmp: Comparison, labels: Optional[Dict[str, str]] = None,
                  issues: Optional[Sequence[primary_review.Issue]] = None, limit: int = 30,
                  shared: Optional[Set[str]] = None, grade: Optional[float] = None) -> str:
    """Собирает комментарий сводного эксперта из расхождений и объективных дефектов.

    Формулировки — по сути ошибки первичного эксперта («не выявлено нарушение по критерию 3.1», «неверно указано
    нарушение по критерию 2.1.5»), а не «не совпало с итоговым». Критерии названы номерами (полные названия — в
    ``differences`` ответа API). Замечания сгруппированы по категориям таксономии
    (:mod:`knowledge_store.primary_taxonomy`): одна строка на категорию — «Пропущенные нарушения: не выявлено нарушение по
    критерию 2.1.3.», «Фактические ошибки: неверно указано нарушение по критерию 1.29; …», «Неполнота: …».

    Args:
        cmp: Результат :func:`compare`.
        labels: ``ключ поля → название критерия`` (из ``pe_form_fields``); без названия выводится ключ.
        issues: Дефекты из :func:`knowledge_store.primary_review.review_opinion` (необязательно).
        limit: Максимум номеров критериев в одной строке; остальные сводятся в «и ещё N».
        shared: Ключи расхождений, общих для всех первичных экспертов (:func:`shared_keys`): в комментарий не попадают —
            ошибкой первичного эксперта они, скорее всего, не являются.
        grade: Предлагаемая оценка: комментарий согласуется с ней. При 1.00 расхождения не перечисляются (по порядку это
            «полное соответствие»; на истории при оценке 1.00 комментарий в 68% случаев — шаблонная фраза), при 0.75 перед
            перечнем стоит «незначительные отклонения», при 0.50 и ниже — «существенные расхождения».

    Returns:
        str: Нумерованный список замечаний; при полном совпадении и отсутствии дефектов — ``POSITIVE_COMMENT``.
    """
    labels = labels or {}
    shared = shared or set()

    def numbers_of(selected: List[Difference]) -> str:
        """Номера критериев через запятую (не больше ``limit``, остальные — «и ещё N»)."""
        nums = [criterion_number(d.key, labels) for d in selected]
        return ", ".join(nums[:limit]) + (f" и ещё {len(nums) - limit}" if len(nums) > limit else "")

    def phrase(selected: List[Difference], one: str, many: str) -> Optional[str]:
        """Фраза в единственном или множественном числе; ``None``, если расхождений нет."""
        return (one if len(selected) == 1 else many).format(numbers_of(selected)) if selected else None

    own = [d for d in cmp.differences if d.key not in shared]
    # шаблоны фраз: (отбор расхождений, в единственном числе, во множественном)
    specs = (
        (lambda d: d.kind == "missed", "Не выявлено нарушение по критерию {}.", "Не выявлены нарушения по критериям: {}."),
        (lambda d: d.kind == "false_alarm", "Неверно указано нарушение по критерию {}.",
         "Неверно указаны нарушения по критериям: {}."),
        # 1 против 2: «не предусмотрено» отмечено там, где требование есть, и наоборот
        (lambda d: d.kind == "other" and d.primary == 2,
         "Неверно отмечено «не предусмотрено» по критерию {} (требование предусмотрено).",
         "Неверно отмечено «не предусмотрено» по критериям: {} (требование предусмотрено)."),
        (lambda d: d.kind == "other" and d.primary != 2,
         "Неверно отмечено «в наличии» по критерию {} (требование не предусмотрено).",
         "Неверно отмечено «в наличии» по критериям: {} (требование не предусмотрено)."),
    )
    by_category: Dict[str, List[str]] = {}          # код категории таксономии → фразы без заглавной буквы и точки
    if grade is None or grade < 1.0:                # оценка 1.00: расхождения остаются в differences, но в комментарий не идут
        for select, one, many in specs:
            for code in primary_taxonomy.COMMENT_ORDER:   # категория расхождения зависит от вида и раздела формы
                text = phrase([d for d in own if select(d) and
                               primary_taxonomy.difference_category(d.kind, d.key, cmp.group) == code], one, many)
                if text:
                    by_category.setdefault(code, []).append(_inline(text))
    for issue in issues or []:
        by_category.setdefault(primary_taxonomy.CODE.get(issue.code, "FORMAL"), []).append(_inline(issue.message))
    # по одной строке на категорию: «Пропущенные нарушения: не выявлено нарушение по критерию 2.1.3.»
    lines: List[str] = [f"{primary_taxonomy.COMMENT_NAMES[code]}: {'; '.join(by_category[code])}."
                        for code in primary_taxonomy.COMMENT_ORDER if code in by_category]
    if lines and grade is not None and grade < 1.0 and any(
            code in by_category for code in ("MISSED_VIOLATION", "FACTUAL", "METHODOLOGICAL", "NORMATIVE")):
        lines.insert(0, "Незначительные отклонения от сводного заключения." if grade >= 0.75
                     else "Существенные расхождения со сводным заключением.")
    if not lines:
        return POSITIVE_COMMENT
    return "\n".join(f"{n}. {text}" for n, text in enumerate(lines, start=1))


def _inline(text: str) -> str:
    """Фраза для строки категории: без заглавной буквы и конечной точки («Не выявлено …» → «не выявлено …»)."""
    text = text.strip().rstrip(".")
    return text[:1].lower() + text[1:]


# ---------------------------------------------------------------------------------------------------------------------
# Обученная модель оценки (многоклассовая логистическая регрессия, коэффициенты — в JSON, вывод на чистом Python).
# Признаки: счётчики расхождений, отдельные расхождения по каждому критерию (у критериев разный вес), сходство текстовых
# полей с итоговым заключением. Файл строит ``scripts/fit_primary_range.py`` по истории оценок.
# ---------------------------------------------------------------------------------------------------------------------
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "primary_range_model.json")
_TEXT_KEYS = ("field1_1", "documents", "field3", "field4")
_TEXT_LIMIT = 1500          # символов для сравнения текстов (SequenceMatcher квадратичен)
_LEN_NORM = 3000
_model_cache: Optional[Dict[str, Any]] = None


def _text_value(data: Dict[str, Any], key: str) -> str:
    """Текст поля заключения (нестроки и пустое → пустая строка)."""
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _similarity(a: str, b: str) -> float:
    """Сходство двух текстов 0–1 (оба пусты → 1, один пуст → 0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a[:_TEXT_LIMIT], b[:_TEXT_LIMIT]).ratio()


def features(primary: Dict[str, Any], summary: Dict[str, Any], cmp: Optional[Comparison] = None) -> Dict[str, float]:
    """Признаки пары «первичное заключение — итоговое» для обученной модели.

    Args:
        primary: ``data`` первичного заключения.
        summary: ``data`` итогового заключения.
        cmp: Готовый результат :func:`compare` (чтобы не считать дважды).

    Returns:
        dict: ``имя признака → число``; расхождения по критериям — ``missed:ключ`` / ``false_alarm:ключ`` / ``other:ключ``.
    """
    cmp = cmp or compare(primary, summary)
    out: Dict[str, float] = {"n_missed": cmp.count("missed"), "n_false_alarm": cmp.count("false_alarm"),
                             "n_other": cmp.count("other"), "match_percent": cmp.match_percent or 0.0}
    for d in cmp.differences:
        out[f"{d.kind}:{d.key}"] = 1.0
    for key in _TEXT_KEYS:
        a, b = _text_value(primary, key), _text_value(summary, key)
        out[f"sim_{key}"] = _similarity(a, b)
        out[f"len_{key}"] = min(len(a), _LEN_NORM) / _LEN_NORM
        out[f"empty_{key}"] = float(not a)
    out["primary_zeros"] = sum(1 for v in comparable(primary).values() if v == 0)
    out["summary_zeros"] = sum(1 for v in comparable(summary).values() if v == 0)
    return out


def load_model(path: str = MODEL_PATH) -> Dict[str, Any]:
    """Читает коэффициенты модели (кэшируется); пустой словарь, если файла нет."""
    global _model_cache
    if _model_cache is None or path != MODEL_PATH:
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            loaded = {}
        if path != MODEL_PATH:
            return loaded
        _model_cache = loaded
    return _model_cache


def predict_range(feats: Dict[str, float], group: str, model: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Оценка и вероятности по обученной модели группы.

    Args:
        feats: Результат :func:`features`.
        group: ``procurement`` или ``report``.
        model: Коэффициенты (по умолчанию из :data:`MODEL_PATH`).

    Returns:
        dict | None: ``{"range": 0.75, "probabilities": {"0.0": .., "0.5": .., "0.75": .., "1.0": ..}}``;
        ``None``, если модели группы нет.
    """
    spec = (model if model is not None else load_model()).get(group)
    if not spec:
        return None
    names, mean, scale = spec["features"], spec["mean"], spec["scale"]
    x = [(feats.get(n, 0.0) - m) / (s or 1.0) for n, m, s in zip(names, mean, scale)]
    logits = [b + sum(w * v for w, v in zip(row, x)) for row, b in zip(spec["coef"], spec["intercept"])]
    top = max(logits)
    exps = [math.exp(v - top) for v in logits]
    total = sum(exps)
    probs = [v / total for v in exps]
    classes = spec["classes"]
    return {"range": classes[probs.index(max(probs))],
            "probabilities": {str(c): round(p, 3) for c, p in zip(classes, probs)}}

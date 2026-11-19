"""Таксономия замечаний к заключению первичного эксперта.

Единый набор категорий (по классификации недостатков, предложенной аналитиком) для всех источников замечаний:
расхождений по критериям (:mod:`knowledge_store.primary_compare`), объективных дефектов
(:mod:`knowledge_store.primary_review`) и замечаний по текстовым полям (:mod:`knowledge_store.primary_remarks`).
Категория показывает, какого рода недостаток; оценку ``range`` она не определяет — её ставит сводный эксперт.
"""
from collections import Counter
from typing import Dict, Iterable, List, Sequence

# код категории → название (формулировки аналитика)
CATEGORIES: Dict[str, str] = {
    "MISSED_VIOLATION": "Существенные пропуски",
    "INCOMPLETE": "Неполнота",
    "FACTUAL": "Фактические ошибки",
    "ARGUMENTATION": "Недостаточная аргументация",
    "CONTRADICTION": "Противоречия",
    "FORMAL": "Формальные недостатки",
}

# вид расхождения с итоговым заключением (``Difference.kind``) → категория
DIFFERENCE_KIND: Dict[str, str] = {
    "missed": "MISSED_VIOLATION",     # не выявлено нарушение, отмеченное в итоговом
    "false_alarm": "FACTUAL",         # указано нарушение, которого в итоговом нет
    "other": "FACTUAL",               # неверно отмечено «в наличии» / «не предусмотрено»
}

# код объективного дефекта (``primary_review.Issue.code``) или замечания (``primary_remarks.Remark.code``) → категория
CODE: Dict[str, str] = {
    "EMPTY_DOCS": "INCOMPLETE", "NO_ITEMS": "INCOMPLETE", "CONCLUSION_TEMPLATE": "INCOMPLETE",
    "ZERO_NO_REMARKS": "ARGUMENTATION", "REMARKS_NO_ZERO": "CONTRADICTION",
    "JUNK_TEXT": "FORMAL", "ITEM_NO_NAME": "FORMAL",
    "DOCS_EMPTY": "INCOMPLETE", "DOCS_MISSING": "INCOMPLETE", "DOCS_EXTRA": "FACTUAL", "DOCS_DATE": "FACTUAL",
    "DOCS_NO_REQUISITES": "FORMAL",
    "SUBJECT_EMPTY": "INCOMPLETE", "SUBJECT_IS_CONCLUSION": "FACTUAL", "SUBJECT_REQUISITES": "FACTUAL",
    "REQ_EMPTY": "INCOMPLETE", "REQ_MISSING": "INCOMPLETE", "REQ_EXTRA": "FACTUAL", "REQ_NO_REFS": "FORMAL",
    "CONCLUSION_POLARITY": "CONTRADICTION",
}


def categorize(difference_kinds: Iterable[str] = (), codes: Iterable[str] = ()) -> List[Dict[str, object]]:
    """Сводка замечаний по категориям таксономии.

    Args:
        difference_kinds: Виды расхождений по критериям (``missed`` / ``false_alarm`` / ``other``), без общих для всех первичных.
        codes: Коды объективных дефектов и замечаний.

    Returns:
        list[dict]: ``{"code", "name", "count"}`` по категориям, в порядке :data:`CATEGORIES`; пустые не включаются.
    """
    counts: Counter = Counter(DIFFERENCE_KIND[k] for k in difference_kinds if k in DIFFERENCE_KIND)
    counts.update(CODE[c] for c in codes if c in CODE)
    return [{"code": code, "name": name, "count": counts[code]} for code, name in CATEGORIES.items() if counts[code]]

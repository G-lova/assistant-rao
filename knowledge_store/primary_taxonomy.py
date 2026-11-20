"""Таксономия замечаний к заключению первичного эксперта.

Девять категорий для всех источников замечаний: расхождений по критериям (:mod:`knowledge_store.primary_compare`),
объективных дефектов (:mod:`knowledge_store.primary_review`) и замечаний по текстовым полям
(:mod:`knowledge_store.primary_remarks`). Категория показывает, какого рода недостаток; оценку ``range`` она не определяет —
её ставит сводный эксперт.

Как определяется каждая категория (принято с Любовью 19.11):

* «Пропуски нарушений» — первичный не выявил нарушение, отмеченное в итоговом; в разделе 1 «Наличие информации…» (закупки)
  также не учтённая информация, имеющаяся в документах, и «не предусмотрено» при наличии требования;
* «Несоответствия» — остальные расхождения по критериям с итоговым заключением (лишнее нарушение, неверное «в наличии»);
* «Фактические ошибки» — значение текстового поля (дата, сумма, номер, реквизиты) отличается от итогового заключения
  (:mod:`knowledge_store.primary_remarks`; подкрепление фактами хранилища — отдельный шаг);
* «Методологические ошибки» — первичный выбрал иной метод обоснования НМЦК (``field2_2_2_0``), чем итоговое
  заключение (:func:`knowledge_store.primary_review.compare_method`); остальное — по смыслу текста (модель);
* «Нормативные ошибки» — неверная ссылка на норму закона в комментарии к нарушению (проверка по загруженным редакциям актов,
  :mod:`risk_monitoring.legal_refs`; пока не подключено);
* «Неполнота» — не заполнен пункт или раздел;
* «Недостаточная аргументация», «Противоречия», «Формальные недостатки» — по смыслу текста (модель; пока не подключено),
  объективные случаи (заглушки, «0» без замечаний) определяются кодом.
"""
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# код категории → название
CATEGORIES: Dict[str, str] = {
    "INCOMPLETE": "Неполнота",
    "FACTUAL": "Фактические ошибки",
    "METHODOLOGICAL": "Методологические ошибки",
    "NORMATIVE": "Нормативные ошибки",
    "ARGUMENTATION": "Недостаточная аргументация",
    "CONTRADICTION": "Противоречия",
    "FORMAL": "Формальные недостатки",
    "MISSED_VIOLATION": "Пропуски нарушений",
    "MISMATCH": "Несоответствия",
}

# Порядок строк в комментарии: как пишут сводные эксперты — сначала пропущенные нарушения и несоответствия критериям,
# затем ошибки в текстовых полях, затем недостатки заключения
COMMENT_ORDER: Tuple[str, ...] = ("MISSED_VIOLATION", "MISMATCH", "INCOMPLETE", "FACTUAL", "METHODOLOGICAL", "NORMATIVE",
                                  "ARGUMENTATION", "CONTRADICTION", "FORMAL")

# Название категории в строке комментария совпадает с CATEGORIES
COMMENT_NAMES: Dict[str, str] = dict(CATEGORIES)

# Закупки: ключ ``field2_<раздел>_…``; раздел 1 — «Наличие информации», разделы 2–4 — «Соответствие … требованиям»
_PROCUREMENT_SECTION = re.compile(r"^field2_(\d+)_")

# код объективного дефекта (``primary_review.Issue.code``) или замечания (``primary_remarks.Remark.code``) → категория
CODE: Dict[str, str] = {
    "EMPTY_DOCS": "INCOMPLETE", "NO_ITEMS": "INCOMPLETE", "CONCLUSION_TEMPLATE": "INCOMPLETE",
    "ZERO_NO_REMARKS": "ARGUMENTATION", "REMARKS_NO_ZERO": "CONTRADICTION",
    "JUNK_TEXT": "FORMAL", "ITEM_NO_NAME": "FORMAL",
    "DOCS_EMPTY": "INCOMPLETE", "DOCS_MISSING": "INCOMPLETE", "DOCS_EXTRA": "FACTUAL", "DOCS_DATE": "FACTUAL",
    "DOCS_NO_REQUISITES": "FORMAL",
    "SUBJECT_EMPTY": "INCOMPLETE", "SUBJECT_IS_CONCLUSION": "FORMAL", "SUBJECT_REQUISITES": "FACTUAL",
    "REQ_EMPTY": "INCOMPLETE", "REQ_MISSING": "INCOMPLETE", "REQ_EXTRA": "FACTUAL", "REQ_NO_REFS": "FORMAL",
    "CONCLUSION_POLARITY": "MISMATCH", "METHOD_MISMATCH": "METHODOLOGICAL",
    "LLM_ARGUMENTATION": "ARGUMENTATION", "LLM_CONTRADICTION": "CONTRADICTION", "LLM_FORMAL": "FORMAL",     # замечания модели
}


def difference_category(kind: str, key: str, group: str = "procurement", primary: Optional[int] = None) -> str:
    """Категория расхождения по критерию.

    Args:
        kind: Вид расхождения (``missed`` / ``false_alarm`` / ``other``).
        key: Ключ поля критерия (``field2_1_12``).
        group: ``procurement`` (закупки) или ``report`` (отчётные документы).
        primary: Значение первичного эксперта (1/0/2); для ``other`` отличает «не предусмотрено» (2) от «в наличии» (1).

    Returns:
        str: Код категории из :data:`CATEGORIES`.
    """
    if kind == "missed":
        return "MISSED_VIOLATION"
    if group == "procurement" and (kind == "false_alarm" or primary == 2):
        match = _PROCUREMENT_SECTION.match(key)
        if match and match.group(1) == "1":
            return "MISSED_VIOLATION"      # раздел 1: информация в документах есть, а первичный её не учёл
    return "MISMATCH"


def categorize(differences: Iterable[Tuple[str, str, Optional[int]]] = (), codes: Iterable[str] = (),
               group: str = "procurement") -> List[Dict[str, object]]:
    """Сводка замечаний по категориям таксономии.

    Args:
        differences: Расхождения по критериям как ``(вид, ключ, значение первичного)``, без общих для всех первичных.
        codes: Коды объективных дефектов и замечаний.
        group: ``procurement`` или ``report``.

    Returns:
        list[dict]: ``{"code", "name", "count"}`` по категориям в порядке :data:`CATEGORIES`; пустые не включаются.
    """
    counts: Counter = Counter(difference_category(kind, key, group, primary) for kind, key, primary in differences)
    counts.update(CODE[c] for c in codes if c in CODE)
    return [{"code": code, "name": name, "count": counts[code]} for code, name in CATEGORIES.items() if counts[code]]

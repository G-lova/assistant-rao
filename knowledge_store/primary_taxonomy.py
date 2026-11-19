"""Таксономия замечаний к заключению первичного эксперта.

Восемь категорий (классификация недостатков, принятая вместе с аналитиком) для всех источников замечаний: расхождений по
критериям (:mod:`knowledge_store.primary_compare`), объективных дефектов (:mod:`knowledge_store.primary_review`) и
замечаний по текстовым полям (:mod:`knowledge_store.primary_remarks`). Категория показывает, какого рода недостаток;
оценку ``range`` она не определяет — её ставит сводный эксперт.

Что может определить код, а что нет. Расхождение по критерию говорит только о том, что первичный эксперт ответил иначе,
чем итоговое заключение, но не о причине. Поэтому категория расхождения выводится из вида расхождения и раздела формы
(см. :func:`difference_category`) — это допущение, оно собрано в одном месте и правится здесь:

* пропуск нарушения (первичный не увидел то, что в итоговом) — «Существенные пропуски»;
* раздел 1 «Наличие информации…» (закупки): первичный не нашёл информацию, которая есть в документах (указал нарушение),
  или отметил «не предусмотрено» там, где требование есть — тоже пропуск («Существенные пропуски»); «в наличии» там, где
  требование не предусмотрено, — «Фактические ошибки» (сведения документов поняты неверно);
* неверный ответ в разделах 2–4 «Соответствие … требованиям законодательства» (закупки) — «Нормативные ошибки»;
* отчётные документы (соответствие результатов, сроков, состава документации требованиям контракта): неверный ответ —
  «Фактические ошибки».

«Методологические ошибки» и «Недостаточная аргументация» по структуре формы определяются слабо (методологические — только
расхождение выбранного метода НМЦК, пока не подключено).
"""
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# код категории → название (в порядке классификации аналитика)
CATEGORIES: Dict[str, str] = {
    "INCOMPLETE": "Неполнота",
    "FACTUAL": "Фактические ошибки",
    "METHODOLOGICAL": "Методологические ошибки",
    "NORMATIVE": "Нормативные ошибки",
    "ARGUMENTATION": "Недостаточная аргументация",
    "CONTRADICTION": "Противоречия",
    "FORMAL": "Формальные недостатки",
    "MISSED_VIOLATION": "Существенные пропуски",
}

# Порядок строк в комментарии: как пишут сводные эксперты — сначала пропущенные нарушения, затем ошибки, затем недостатки заключения
COMMENT_ORDER: Tuple[str, ...] = ("MISSED_VIOLATION", "INCOMPLETE", "FACTUAL", "METHODOLOGICAL", "NORMATIVE",
                                  "ARGUMENTATION", "CONTRADICTION", "FORMAL")

# Название категории в строке комментария. Совпадает с CATEGORIES, кроме пропуска нарушений: «существенные» в начале строки
# противоречило бы заголовку «Незначительные отклонения» при оценке 0.75, степень тяжести задаёт оценка
COMMENT_NAMES: Dict[str, str] = {**CATEGORIES, "MISSED_VIOLATION": "Пропущенные нарушения"}

# Закупки: ключ ``field2_<раздел>_…``; раздел 1 — «Наличие информации», разделы 2–4 — «Соответствие … требованиям»
_PROCUREMENT_SECTION = re.compile(r"^field2_(\d+)_")

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
    if group == "procurement":
        match = _PROCUREMENT_SECTION.match(key)
        if match and match.group(1) != "1":
            return "NORMATIVE"
        if match and (kind == "false_alarm" or primary == 2):
            return "MISSED_VIOLATION"      # раздел 1: информация в документах есть, а первичный её не учёл
    return "FACTUAL"


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

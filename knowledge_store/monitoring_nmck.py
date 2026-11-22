"""Прототип проверки критерия мониторинга «Соблюдение требований в части обоснования НМЦК/цены договора».

Работает по тексту документов одной закупки (контракта/договора). Текст получает вызывающий код
(``pdftotext`` или распознавание сканов, см. ``scripts/check_monitoring_nmck.py``), модуль только
классифицирует документы и применяет правила. Правила выведены из итоговых заключений мониторинга и
подлежат проверке на отложенной выборке организаций (``scripts/eval_monitoring_nmck.py``).

Вердикт каждого правила подкрепляется цитатой: подряд идущими словами из текста документа
(по аналогии с проверкой цитат в ``primary_llm_review``), без цитаты замечание не выдаётся.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Типы документов закупки, определяемые по имени файла и началу текста
DOC_NMCK = "nmck_justification"       # обоснование НМЦК / расчёт цены
DOC_OFFER = "price_offer"             # коммерческое (ценовое) предложение
DOC_MEMO = "memo"                     # служебная записка (может содержать расчёт)
DOC_OTHER = "other"

# Признаки типа по имени файла (регистр не важен); порядок — приоритет
_NAME_RULES = [
    (DOC_NMCK, r"обоснован\w*\s*(нмц|цен)|нмцк|нмцд|расч[её]т"),
    (DOC_OFFER, r"коммерческ|ценов\w+ предложен|\bкп\s*\d*\b"),
    (DOC_MEMO, r"служебн\w+ записк"),
]
# Файлы, которые заведомо не относятся к обоснованию цены: распознавать их не нужно
_SKIP_NAME = re.compile(r"контракт|договор|акт|плат[её]ж|оплат|счет|счёт|приемк|приёмк|протокол|заявк|уведомлен|письм", re.I)

# Признаки в тексте
_RE_NMCK_TITLE = re.compile(r"обоснование\s+(начальной|цены|нмц)", re.I)
_RE_ORDER45 = re.compile(r"15\.02\.2021\s*(г\.?)?\s*(№|n)?\s*45", re.I)
_RE_REGISTRY = re.compile(r"реестр\w*\s+(контракт|договор)|реестровая запись", re.I)
_RE_OFFER_REQUEST = re.compile(r"направлены?\s+запрос\w*|запрос\w*\s+о\s+предоставлении\s+информации", re.I)
_RE_OFFERS_NONE = re.compile(r"(коммерческие|ценовые)\s+предложения\s+не\s+получены", re.I)
_RE_AUTHOR_DATE = re.compile(
    r"(дата\s+(подготовки|проведения|составления)[^.\n]{0,80}\d{2}\.\d{2}\.\d{4}|"
    r"(специалист|исполнитель|подготовил|составил)[^\n]{0,80}\d{2}\.\d{2}\.\d{4}|"
    r"\d{2}\.\d{2}\.\d{4}[^\n]{0,60}(специалист|исполнитель))", re.I)


@dataclass
class Doc:
    """Документ закупки: имя файла, текст (``None`` — не читался) и определённый тип."""
    name: str
    text: Optional[str] = None
    kind: str = DOC_OTHER


@dataclass
class Finding:
    """Результат одного правила: код, формулировка нарушения и цитата из документа."""
    rule: str
    message: str
    files: List[str] = field(default_factory=list)
    quote: str = ""


def norm(text: str) -> str:
    """Сжимает пробелы и переводы строк — для поиска цитат и подстрок."""
    return re.sub(r"\s+", " ", text or "").strip()


def should_skip_by_name(name: str) -> bool:
    """Нужно ли пропустить файл без распознавания (контракты, акты, платёжки и т. п.).

    Файл, в имени которого есть признак обоснования цены или КП, не пропускается, даже если
    в имени встречается «договор» («Расчет и КП», «Обоснование НМЦК 1121»).
    """
    if any(re.search(p, name, re.I) for _, p in _NAME_RULES):
        return False
    return bool(_SKIP_NAME.search(name))


def classify(doc: Doc) -> str:
    """Определяет тип документа по имени файла, а при его неинформативности — по началу текста."""
    for kind, pattern in _NAME_RULES:
        if re.search(pattern, doc.name, re.I):
            return kind
    head = norm((doc.text or "")[:600])
    if _RE_NMCK_TITLE.search(head):
        return DOC_NMCK
    return DOC_OTHER


def _quote(text: str, match: "re.Match", width: int = 160) -> str:
    """Цитата вокруг найденного фрагмента — подряд идущие слова нормализованного текста."""
    start = max(match.start() - 20, 0)
    return norm(text[start:match.end() + width])[:width + 40]


def check_purchase(docs: List[Doc]) -> Dict:
    """Применяет правила к комплекту документов одной закупки.

    Правила:
        * ``NO_JUSTIFICATION`` — есть коммерческие предложения/служебная записка, но нет документа
          обоснования НМЦК (заключения: «обоснование цены не проведено, представлены только КП»);
        * ``COMBINED_METHODS_ORDER45`` — обоснование по Приказу Росгвардии № 45 одновременно опирается
          на запросы КП и на поиск в реестре контрактов (заключения: «НМЦК определено некорректно»);
        * ``NO_AUTHOR_DATE`` — в обосновании нет даты и исполнителя расчёта; вердикт НЕ меняет и попадает
          в ``notes``: типовой расчёт по форме Приказа № 45 (Пятигорский ГУ) их не содержит, а заключение
          признаёт его соответствующим, поэтому как нарушение правило давало бы ложные срабатывания.

    Args:
        docs: Документы закупки; у документов без текста ``text=None`` (пропущены по имени).

    Returns:
        dict: ``verdict`` («Соответствует» / «Не соответствует» / «Недостаточно данных»),
        ``findings`` (список :class:`Finding` в виде словарей), ``notes`` (замечания без влияния на
        вердикт) и ``docs`` (типы документов).
    """
    for d in docs:
        d.kind = classify(d)
    justif = [d for d in docs if d.kind == DOC_NMCK and d.text]
    offers = [d for d in docs if d.kind in (DOC_OFFER, DOC_MEMO)]
    findings: List[Finding] = []
    notes: List[Finding] = []

    if not justif:
        if offers:
            findings.append(Finding("NO_JUSTIFICATION",
                                    "Документ обоснования цены в комплекте отсутствует, представлены только "
                                    "коммерческие предложения/служебная записка",
                                    [d.name for d in offers]))
        verdict = "Не соответствует" if findings else "Недостаточно данных"
        return _result(verdict, findings, docs, notes)

    for d in justif:
        t = d.text or ""
        n = norm(t)
        m45, mreg = _RE_ORDER45.search(n), _RE_REGISTRY.search(n)
        if m45 and mreg and (_RE_OFFER_REQUEST.search(n) or _RE_OFFERS_NONE.search(n)):
            findings.append(Finding("COMBINED_METHODS_ORDER45",
                                    "НМЦК определена некорректно: обоснование ссылается на Приказ Росгвардии № 45, "
                                    "но совмещает запросы КП и поиск в реестре контрактов",
                                    [d.name], _quote(n, mreg)))
        if not _RE_AUTHOR_DATE.search(n):
            notes.append(Finding("NO_AUTHOR_DATE",
                                    "В обосновании не указаны дата подготовки и исполнитель расчёта", [d.name]))
    return _result("Не соответствует" if findings else "Соответствует", findings, docs, notes)


def _result(verdict: str, findings: List[Finding], docs: List[Doc], notes: Optional[List[Finding]] = None) -> Dict:
    """Собирает ответ: вердикт, замечания, пометки и типы документов (для отладки и отчёта)."""
    return {"verdict": verdict,
            "findings": [f.__dict__ for f in findings],
            "notes": [f.__dict__ for f in (notes or [])],
            "docs": [{"name": d.name, "kind": d.kind, "read": d.text is not None} for d in docs]}

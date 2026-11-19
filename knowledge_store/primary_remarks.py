"""Замечания к заключению первичного эксперта по отчётным документам (объект 7) — сравнение текстовых полей.

Сводные эксперты в комментариях к оценке чаще всего пишут не про отдельные критерии, а про само заполнение заключения
(на 2,0 тыс. комментариев по отчётным документам): «перечислены не все требования к составу отчётной документации»
(49%), «перечень документов неполный / без реквизитов» (29%), «предмет экспертизы указан неверно» (20%), «выводы не
соответствуют», «ошибки в датах». Модуль находит такие замечания детерминированно, сравнивая поля первичного
заключения с итоговым сводным и (если переданы) с названиями загруженных документов экспертизы (``pe_documents``):

* ``DOCS_MISSING`` / ``DOCS_EXTRA`` / ``DOCS_DATE`` / ``DOCS_NO_REQUISITES`` — перечень документов ``field1_1``;
* ``SUBJECT_EMPTY`` / ``SUBJECT_IS_CONCLUSION`` / ``SUBJECT_REQUISITES`` — предмет экспертизы ``field2_1``;
* ``REQ_EMPTY`` / ``REQ_MISSING`` / ``REQ_EXTRA`` / ``REQ_NO_REFS`` — требования к составу отчётной документации
  (``requirements`` позиций ``field1_3``);
* ``CONCLUSION_POLARITY`` — вывод о принятии результатов (``field5_3_3`` / ``field6_3_3``).

«Не раскрыто, в чём несоответствие» (поле пояснения есть, но суть не названа) и «не аргументирует» по тексту
определить нельзя — это задача модели или эксперта. Тексты замечаний — в формулировках самих сводных экспертов.
"""
import re
from dataclasses import dataclass, field
from configs import settings
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MINOR = "minor"
MAJOR = "major"

_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
           "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")
_DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})", re.I)
_NUMBER = re.compile(r"(?:№|\bN\b|\bNo\b\.?)\s*([\w\-/]+)", re.I)

# Виды документов по названию. Порядок важен: частные виды раньше общих («счёт-фактура» раньше «счёта»).
_KINDS: List[Tuple[str, "re.Pattern"]] = [
    ("дополнительное соглашение", re.compile(r"дополнительн\w+\s+соглашени", re.I)),
    ("договор", re.compile(r"договор|контракт", re.I)),
    ("счёт-фактура", re.compile(r"сч[её]т[\s-]*фактур", re.I)),
    ("УПД", re.compile(r"универсальн\w+[\s-]+передаточн|\bупд\b", re.I)),
    ("накладная", re.compile(r"накладн", re.I)),
    ("документ о приёмке", re.compile(r"документ\w*[\s-]+о[\s-]+приемк|документ\w*[\s-]+о[\s-]+приёмк", re.I)),
    ("акт", re.compile(r"\bакт\w*\b", re.I)),
    ("платёжное поручение", re.compile(r"платеж\w+[\s-]+поручени|платёж\w+[\s-]+поручени", re.I)),
    ("счёт", re.compile(r"сч[её]т(?![\s-]*фактур)", re.I)),
    ("спецификация", re.compile(r"спецификац", re.I)),
]

# Требования к составу отчётной документации: канонические виды (для сравнения наборов, а не дословных строк)
_REQ_KINDS: List[Tuple[str, "re.Pattern"]] = [
    ("акт сдачи-приёмки", re.compile(r"\bакт\w*\b", re.I)),
    ("товарная накладная", re.compile(r"накладн", re.I)),
    ("счёт-фактура", re.compile(r"сч[её]т[\s-]*фактур", re.I)),
    ("УПД", re.compile(r"универсальн\w+[\s-]+передаточн|\bупд\b", re.I)),
    ("счёт на оплату", re.compile(r"сч[её]т(?![\s-]*фактур)", re.I)),
    ("техническая / эксплуатационная документация", re.compile(r"эксплуатационн|паспорт|инструкци\w+ по эксплуатации|руководств\w+ пользовател|технической документации", re.I)),
    ("документы о соответствии (сертификат, декларация)", re.compile(r"сертификат|деклараци\w+ о соответствии|оценк\w+ соответствия|соответстви\w+ ТР", re.I)),
    ("гарантийные документы", re.compile(r"гарантийн", re.I)),
    ("платёжное поручение", re.compile(r"платеж\w+[\s-]+поручени|платёж\w+[\s-]+поручени", re.I)),
]
# Слово перед названием документа, после которого это ссылка, а не сам документ: «к договору», «по акту»
_REFERENCE_PREFIX = re.compile(r"(?:\bк|\bпо|\bв|\bна|\bсогласно|\bс|\bот)\s+$", re.I)
_REFS_RE = re.compile(r"\bп\.|\bпп\.|пункт|приложени|раздел|ст\.|статья", re.I)
_CONCLUSION_WORDS = re.compile(r"полное соответствие|не соответствует|соответствует|рекомендую|рекомендуется|принять|выводы?\b", re.I)
_SUBJECT_START = re.compile(r"^\s*оценка\b", re.I)
_CONCLUSION_KEY = re.compile(r"^field[56]_3_3$")
_REJECT = re.compile(r"не рекоменду\w*\s+принимать|не принимать|отказать|не принима\w+|не принят", re.I)
_ACCEPT = re.compile(r"принять|принят|принима", re.I)


@dataclass
class Remark:
    """Замечание к заключению первичного эксперта.

    Attributes:
        code: Код замечания (``DOCS_MISSING`` …).
        severity: ``minor`` или ``major``.
        text: Формулировка для комментария (в стиле сводных экспертов).
        target: Поля заключения, к которым относится замечание.
        evidence: Данные, на которых основано замечание (для проверки экспертом).
    """

    code: str
    severity: str
    text: str
    target: List[str] = field(default_factory=list)
    evidence: Dict[str, object] = field(default_factory=dict)


@dataclass
class DocRef:
    """Документ из перечня: вид, номер и дата (нормализованные) и исходная строка."""

    kind: str
    number: Optional[str]
    date: Optional[str]
    raw: str

    def display(self) -> str:
        """Короткая запись для комментария: ``Товарная накладная № 308 от 27.06.2024``."""
        parts = [self.kind[:1].upper() + self.kind[1:]]
        if self.number:
            parts.append(f"№ {self.number}")
        if self.date:
            parts.append(f"от {self.date}")
        return " ".join(parts)


def parse_date(text: str) -> Optional[str]:
    """Первая дата в тексте в виде ``дд.мм.гггг`` (``23.05.24``, ``23 мая 2024г`` → ``23.05.2024``); иначе ``None``."""
    found = []
    for m in _DATE_NUM.finditer(text):
        day, month, year = m.groups()
        found.append((m.start(), int(day), int(month), int(year) + (2000 if len(year) == 2 else 0)))
    for m in _DATE_WORD.finditer(text):
        found.append((m.start(), int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))))
    if not found:
        return None
    _, day, month, year = min(found)
    return f"{day:02d}.{month:02d}.{year}" if 1 <= month <= 12 and 1 <= day <= 31 else None


def find_dates(text: Optional[str]) -> Set[str]:
    """Все даты в тексте в виде ``дд.мм.гггг`` (``23.05.24``, ``23 мая 2024г`` → ``23.05.2024``); пусто, если дат нет."""
    found: Set[str] = set()
    for m in _DATE_NUM.finditer(text or ""):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year += 2000 if year < 100 else 0
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.add(f"{day:02d}.{month:02d}.{year}")
    for m in _DATE_WORD.finditer(text or ""):
        day = int(m.group(1))
        if 1 <= day <= 31:
            found.add(f"{day:02d}.{_MONTHS[m.group(2).lower()]:02d}.{m.group(3)}")
    return found


# Денежная сумма: «1 514 970,00», «1514970.00», «920170,00 рублей»; без десятичной части — не меньше 5 цифр (не номера и годы)
_AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[\s\u00a0]\d{3})+(?:[.,]\d{1,2})?|\d{5,}(?:[.,]\d{1,2})?|\d+[.,]\d{2})(?![\d])")


def find_amounts(text: Optional[str]) -> Set[float]:
    """Суммы в тексте (числа с разрядами и копейками), округлённые до копеек; даты и номера документов не считаются суммами."""
    cleaned = _DATE_NUM.sub(" ", text or "")
    out: Set[float] = set()
    for m in _AMOUNT.finditer(cleaned):
        raw = re.sub(r"[\s\u00a0]", "", m.group(1)).replace(",", ".")
        try:
            out.add(round(float(raw), 2))
        except ValueError:
            continue
    return out


def find_numbers(text: Optional[str]) -> Set[str]:
    """Номера после «№» / «N» (нормализованные: без пробелов, регистра, ведущих нулей и знака «б/н»)."""
    return {n for n in (normalize_number(m.group(1)) for m in _NUMBER.finditer(text or "")) if n}


def normalize_number(raw: Optional[str]) -> Optional[str]:
    """Номер документа без пробелов, регистра и хвостовой пунктуации (``№201,`` → ``201``)."""
    if not raw:
        return None
    value = raw.strip(" .,;:()").replace(" ", "").lower()
    if value in ("б/н", "бн", "-"):                     # «без номера» — номера нет
        return None
    return value.lstrip("0") or None


def parse_documents(text: Optional[str]) -> List[DocRef]:
    """Разбирает перечень документов (``field1_1`` или названия загруженных файлов) на документы с видом, номером и датой.

    Текст режется по вхождениям названий видов документов (договор, накладная, акт, счёт …): так «Договор № 1 от
    23 мая 2024г, товарная накладная №201 от 23.05.2024» даёт два документа независимо от разделителей и нумерации.

    Args:
        text: Перечень документов.

    Returns:
        list[DocRef]: Документы в порядке упоминания.
    """
    if not text:
        return []
    hits = []
    for kind, pattern in _KINDS:
        for m in pattern.finditer(text):
            hits.append((m.start(), m.end(), kind))
    hits.sort()
    # вхождения, вложенные в уже найденный документ («счёт» в «счёт-фактура»), отбрасываем; так же — упоминания внутри
    # скобок («УПД (счёт-фактура и передаточный документ)») и ссылки на документ («по договору», «к контракту»)
    chosen: List[Tuple[int, int, str]] = []
    for hit in hits:
        if chosen and hit[0] < chosen[-1][1]:
            continue
        before = text[:hit[0]]
        if before.count("(") > before.count(")") or _REFERENCE_PREFIX.search(before):
            continue
        chosen.append(hit)
    docs = []
    for i, (start, end, kind) in enumerate(chosen):
        segment = text[start:(chosen[i + 1][0] if i + 1 < len(chosen) else len(text))]
        number = _NUMBER.search(segment)
        docs.append(DocRef(kind, normalize_number(number.group(1)) if number else None, parse_date(segment),
                           re.sub(r"\s+", " ", segment).strip(" ,;.\n")))
    return docs


# Документы о приёмке (акт, накладная, УПД со счётом-фактурой, документ о приёмке в ЕИС) в перечнях записывают по-разному
# и взаимозаменяемо («УПД/акт № 3»), поэтому при сравнении они считаются одной группой
_KIND_GROUP = {"УПД": "приёмка", "счёт-фактура": "приёмка", "накладная": "приёмка", "акт": "приёмка",
               "документ о приёмке": "приёмка"}


def _same(a: DocRef, b: DocRef) -> bool:
    """Один и тот же документ: тот же вид и не противоречащие друг другу номер и дата.

    Если у одного из документов номера (или даты) нет, они считаются одним документом: эксперт мог записать короче, это
    не пропуск. Противоречие — когда номера (или даты) указаны у обоих и различаются.
    """
    if _KIND_GROUP.get(a.kind, a.kind) != _KIND_GROUP.get(b.kind, b.kind):
        return False
    if a.number and b.number:
        return a.number == b.number
    if a.date and b.date:
        return a.date == b.date
    return True


def compare_documents(primary: str, summary: Optional[str], uploaded: Sequence[str] = ()) -> List[Remark]:
    """Сравнивает перечень документов первичного эксперта с итоговым и с названиями загруженных документов.

    Опорный набор — итоговый перечень и документы экспертизы (``pe_documents``): документ из любого из них, не указанный
    первичным экспертом, — пропуск; документ первичного эксперта, которого нет ни там, ни там, — лишний. Если номера
    совпали, а даты разные — ошибка в реквизитах.

    Args:
        primary: ``field1_1`` первичного заключения.
        summary: ``field1_1`` итогового заключения (может быть пустым).
        uploaded: Названия загруженных документов экспертизы.

    Returns:
        list[Remark]: ``DOCS_MISSING``, ``DOCS_EXTRA``, ``DOCS_DATE``, ``DOCS_NO_REQUISITES`` (по найденным).
    """
    mine = parse_documents(primary)
    reference = parse_documents(summary)
    for title in uploaded:
        reference += [d for d in parse_documents(title) if d.kind not in ("договор", "дополнительное соглашение") or d.number]
    out: List[Remark] = []
    if not mine and not reference:
        return out
    if not mine:
        return [Remark("DOCS_EMPTY", MAJOR, "Не указан перечень документов, поступивших на экспертизу.", ["field1_1"])]
    missing = [r for r in reference if not any(_same(r, m) for m in mine)]
    seen: List[DocRef] = []
    for r in missing:                                              # один документ из двух источников — одна запись
        if not any(_same(r, s) for s in seen):
            seen.append(r)
    extra = [m for m in mine if reference and not any(_same(m, r) for r in reference)]
    date_wrong = []
    for m in mine:
        for r in reference:
            if m.kind == r.kind and m.number and m.number == r.number and m.date and r.date and m.date != r.date:
                date_wrong.append((m, r))
                break
    if len(seen) >= getattr(settings, "PRIMARY_REVIEW_DOCS_MISSING_MIN", 3):
        out.append(Remark("DOCS_MISSING", MAJOR, "Перечень документов, поступивших на экспертизу, указан не полностью; не указаны: "
                          + "; ".join(d.display() for d in seen[:8]) + ("; …" if len(seen) > 8 else "") + ".",
                          ["field1_1"], {"missing": [d.display() for d in seen]}))
    if extra:
        out.append(Remark("DOCS_EXTRA", MINOR, "В перечне документов указаны документы, которых нет в комплекте: "
                          + "; ".join(d.display() for d in extra[:8]) + ".", ["field1_1"], {"extra": [d.display() for d in extra]}))
    if date_wrong:
        out.append(Remark("DOCS_DATE", MINOR, "Неверно указаны реквизиты документов: " + "; ".join(
            f"{m.kind} № {m.number} (указана дата {m.date}, в документе {r.date})" for m, r in date_wrong[:6]) + ".",
            ["field1_1"], {"wrong": [(m.display(), r.display()) for m, r in date_wrong]}))
    bare = [m for m in mine if not m.number and not m.date]
    if bare and len(bare) == len(mine):
        out.append(Remark("DOCS_NO_REQUISITES", MINOR, "Документы в перечне указаны без реквизитов (номер, дата).", ["field1_1"]))
    return out


def compare_subject(primary: Optional[str], summary: Optional[str]) -> List[Remark]:
    """Предмет экспертизы (``field2_1``): пустой, подменён выводом или реквизиты договора расходятся с итоговым.

    Args:
        primary: ``field2_1`` первичного заключения.
        summary: ``field2_1`` итогового заключения.
    """
    text = (primary or "").strip()
    if not text:
        return [Remark("SUBJECT_EMPTY", MAJOR, "Не указан предмет экспертизы.", ["field2_1"])]
    if not _SUBJECT_START.match(text) and _CONCLUSION_WORDS.search(text):
        return [Remark("SUBJECT_IS_CONCLUSION", MAJOR, "Предмет экспертизы указан неверно: вместо предмета приведён вывод.", ["field2_1"])]
    if summary:
        a, b = parse_documents(text), parse_documents(summary)
        main_a = next((d for d in a if d.kind == "договор"), None)
        main_b = next((d for d in b if d.kind == "договор"), None)
        if main_a and main_b and ((main_a.number and main_b.number and main_a.number != main_b.number)
                                  or (main_a.date and main_b.date and main_a.date != main_b.date)):
            return [Remark("SUBJECT_REQUISITES", MINOR, "В предмете экспертизы неверно указаны реквизиты договора.", ["field2_1"],
                           {"primary": main_a.display(), "summary": main_b.display()})]
    return []


def requirement_text(data: Dict[str, object]) -> str:
    """Все тексты ``requirements`` позиций ``field1_3`` заключения одной строкой."""
    parts = []
    for item in data.get("field1_3") or []:
        if isinstance(item, dict) and isinstance(item.get("requirements"), str):
            parts.append(item["requirements"])
    return "\n".join(parts).strip()


def requirement_kinds(text: str) -> Set[str]:
    """Канонические виды документов, упомянутые в требованиях к составу отчётной документации."""
    return {name for name, pattern in _REQ_KINDS if pattern.search(text)}


def compare_requirements(primary: Dict[str, object], summary: Dict[str, object]) -> List[Remark]:
    """Требования к составу отчётной документации: набор видов документов, ссылки на пункты договора.

    Args:
        primary: ``data`` первичного заключения.
        summary: ``data`` итогового заключения.
    """
    mine, ref = requirement_text(primary), requirement_text(summary)
    if not ref and not mine:
        return []
    if ref and not mine:
        return [Remark("REQ_EMPTY", MAJOR, "Не указаны требования к составу представления отчётной документации, предусмотренные договором.",
                       ["field1_3[].requirements"])]
    out: List[Remark] = []
    a, b = requirement_kinds(mine), requirement_kinds(ref)
    if b - a:
        out.append(Remark("REQ_MISSING", MAJOR, "Перечислены не все требования к составу представления отчётной документации, предусмотренные договором; "
                          "не указаны: " + "; ".join(sorted(b - a)) + ".", ["field1_3[].requirements"], {"missing": sorted(b - a)}))
    if a - b and ref:
        out.append(Remark("REQ_EXTRA", MINOR, "В требованиях к составу отчётной документации указаны документы, не предусмотренные договором: "
                          + "; ".join(sorted(a - b)) + ".", ["field1_3[].requirements"], {"extra": sorted(a - b)}))
    if ref and _REFS_RE.search(ref) and not _REFS_RE.search(mine):
        out.append(Remark("REQ_NO_REFS", MINOR, "Не приведены ссылки на пункты договора при перечислении требований к составу отчётной документации.",
                          ["field1_3[].requirements"]))
    return out


def polarity(text: Optional[str]) -> Optional[str]:
    """Вывод о приёмке результатов: ``reject`` / ``accept`` / ``None`` (в тексте не определён)."""
    if not text:
        return None
    if _REJECT.search(text):
        return "reject"
    return "accept" if _ACCEPT.search(text) else None


def compare_conclusion(primary: Dict[str, object], summary: Dict[str, object]) -> List[Remark]:
    """Вывод о принятии результатов (``field5_3_3`` / ``field6_3_3``) расходится с итоговым."""
    for key in sorted(k for k in primary if _CONCLUSION_KEY.match(k)):
        a, b = polarity(str(primary.get(key) or "")), polarity(str(summary.get(key) or ""))
        if a and b and a != b:
            return [Remark("CONCLUSION_POLARITY", MAJOR, "Вывод о принятии результатов не соответствует рассмотренным документам.", [key],
                           {"primary": a, "summary": b})]
    return []


def review_report(primary: Dict[str, object], summary: Dict[str, object], uploaded: Sequence[str] = ()) -> List[Remark]:
    """Все замечания по текстовым полям заключения отчётных документов.

    Args:
        primary: ``data`` первичного заключения.
        summary: ``data`` итогового заключения.
        uploaded: Названия загруженных документов экспертизы (``pe_documents``), необязательно.

    Returns:
        list[Remark]: Замечания: существенные (``major``) первыми.
    """
    remarks = (compare_documents(str(primary.get("field1_1") or ""), str(summary.get("field1_1") or ""), uploaded)
               + compare_subject(str(primary.get("field2_1") or ""), str(summary.get("field2_1") or ""))
               + compare_requirements(primary, summary) + compare_conclusion(primary, summary))
    return sorted(remarks, key=lambda r: r.severity != MAJOR)

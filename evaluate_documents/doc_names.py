"""Наименование документа (``raw_data.document_name.value``) в едином виде «Вид от ДД.ММ.ГГГГ № номер».

Раньше название бралось как есть из ответа модели: «ТОВАРНАЯ НАКЛАДНАЯ», «СЧЕТ-ФАКТУРА № 5486 ОТ 24 марта 2026 г.»,
«146 Договор Медипал-ОНКО энтивио.docx» (имя вложения из XML), а для печатных форм ЕИС без расширения в имени файла —
«Бинарный файл tmp61wjkjn9 (формат .-15406010760260001600001))»: файл не читался, и модель «называла» служебную строку
(экспертиза с ``risk_monitoring_contract_id`` 8865, 19.10.2026).

Модель получает в промпте образец названия и имя файла, а здесь название доводится кодом:

* даты — ``ДД.ММ.ГГГГ`` («2026-03-24», «24 марта 2026 г.» → «24.03.2026»), «N 146» → «№ 146», «№ 5486 от 24.03.2026» →
  «от 24.03.2026 № 5486»;
* название целиком заглавными буквами — с заглавной только первой буквы (аббревиатуры остаются);
* реквизиты — если модель их не написала, а они есть: «Товарная накладная» + шапка формы «Номер документа 5486,
  Дата составления 24.03.2026» → «Товарная накладная от 24.03.2026 № 5486»; электронный контракт — номер договора
  и дата заключения из извлечённых данных;
* формат файла — дописывается: «(формат XML)», «, печатная форма ЕИС» / «(печатная форма)», «(архив XML, HTML и файлов
  электронной подписи)»; модель формат не пишет;
* служебная строка, имя временного файла или пустое название — заменяются названием из имени файла.
"""
import re
from typing import Any, Dict, List, Optional

# Начало текста, который FileReader возвращает для неподдерживаемого формата
UNSUPPORTED_PREFIXES = ("Бинарный файл", "Файл не прочитан")

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_MONTH_RE = r"(январ[яь]|феврал[яь]|марта?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|августа?|сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])"
# Аббревиатуры, которые остаются заглавными при переводе названия из ВЕРХНЕГО регистра
ABBREVIATIONS = ("ЕИС", "УПД", "НДС", "ООО", "ОАО", "ЗАО", "ПАО", "АО", "ИП", "РФ", "ФЗ", "ГК", "КС", "ТОРГ", "ТН",
                 "ИНН", "КПП", "ОГРН", "ЖНВЛП", "НМЦК", "ТЗ", "ГОСТ", "ТУ", "СРО", "ЭЦП", "XML", "HTML", "PDF", "РУ",
                 "ЛП", "ФГАОУ", "ФГБУ", "ГБУЗ", "ГАУЗ", "МБОУ", "МКУ", "ФКУ", "УФК")
FILE_EXT_RE = re.compile(r"\.(docx?|pdf|xlsx?|xml|html?|zip|rar|7z|rtf|odt|txt|jpe?g|png|tiff?|sig)\s*$", re.I)
TMP_RE = re.compile(r"\btmp[a-z0-9_]{5,}\b", re.I)
# Признаки печатной формы ЕИС
EIS_RE = re.compile(r"zakupki\.gov\.ru|единой\s+информационной\s+систем|реестров\w*\s+номер\s+контракт", re.I)
ARCHIVE_ENTRY_RE = re.compile(r"^=== (.+?) ===$", re.M)
ARCHIVE_LABELS = (  # расширение вложения → как назвать в скобках, по порядку упоминания
    ((".xml",), "XML"), ((".html", ".htm"), "HTML"), ((".pdf",), "PDF"), ((".doc", ".docx", ".rtf", ".odt"), "Word"),
    ((".xls", ".xlsx"), "Excel"), ((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif"), "изображений"),
    ((".sig", ".sgn", ".p7s", ".p7m", ".sign"), "файлов электронной подписи"),
)


def is_unsupported_text(text: Optional[str]) -> bool:
    """Текст — служебная строка FileReader о неподдерживаемом формате, а не содержимое документа."""
    return bool(text) and str(text).lstrip().startswith(UNSUPPORTED_PREFIXES)


def name_from_filename(filename: str) -> str:
    """Название из имени файла: без расширения, «-»/«_» → пробелы, с заглавной буквы.

    «Печатная-форма-исполнения-контракта-№-1540601076026000160-(версия-0,-док.-15406010760260001600014)» →
    «Печатная форма исполнения контракта № 1540601076026000160 (версия 0, док. 15406010760260001600014)».

    Args:
        filename: Имя файла.

    Returns:
        str: Название (пустое для пустого имени).
    """
    base = re.split(r"[\\/]", filename or "")[-1]
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)   # любое настоящее расширение (.pdf, .dwg …)
    base = re.sub(r"[-_]+", " ", base)
    base = re.sub(r"\s+,", ",", re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", base)))
    base = re.sub(r"\s{2,}", " ", base).strip(" .")
    return base[:1].upper() + base[1:]


def _month(word: str) -> int:
    """Номер месяца по слову («марта» → 3)."""
    return next(v for k, v in MONTHS.items() if word.lower().startswith(k))


def _sentence_case(name: str) -> str:
    """«ТОВАРНАЯ НАКЛАДНАЯ» → «Товарная накладная»; аббревиатуры (УПД, НДС, ООО…) остаются заглавными."""
    letters = [ch for ch in name if ch.isalpha()]
    if len(letters) < 4 or sum(ch.isupper() for ch in letters) / len(letters) < 0.7:
        return name
    out = name.lower()
    for abbr in ABBREVIATIONS:
        out = re.sub(rf"(?<![\w]){abbr.lower()}(?![\w])", abbr, out)
    out = re.sub(r"«([а-яё])", lambda m: "«" + m.group(1).upper(), out)   # название в кавычках — с заглавной
    return out[:1].upper() + out[1:]


def normalize_name(name: str) -> str:
    """Приводит название к виду «Вид от ДД.ММ.ГГГГ № номер» (даты, «№», регистр, порядок «от … №»).

    Args:
        name: Название от модели.

    Returns:
        str: Название после правок.
    """
    s = re.sub(r"\s+", " ", str(name or "")).strip().strip(".").strip()
    s = re.sub(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", r"\3.\2.\1", s)
    s = re.sub(rf"(?<!\d)(\d{{1,2}})\s+{_MONTH_RE}\s+(\d{{4}})(\s*(?:г\.|года|г(?![а-яё])))?",
               lambda m: f"{int(m.group(1)):02d}.{_month(m.group(2)):02d}.{m.group(3)}", s, flags=re.I)
    s = re.sub(r"(\d{2}\.\d{2}\.\d{4})\s*г\.?(?![а-яё])", r"\1", s)
    s = re.sub(r"(?<![\w№])(?:N|No|N°)\s*(?=\d)", "№ ", s)
    s = re.sub(r"№(?=[\wА-ЯЁа-яё(])", "№ ", s)
    s = _sentence_case(s)
    s = re.sub(r"\bОТ\b", "от", s)
    # «№ 5486 от 24.03.2026» → «от 24.03.2026 № 5486»
    s = re.sub(r"№\s*([^\s,]+)\s+от\s+(\d{2}\.\d{2}\.\d{4})", r"от \2 № \1", s)
    return s[:1].upper() + s[1:] if s else s


def is_weak_name(name: Optional[str], filename: str = "") -> bool:
    """Название непригодно: пустое, служебная строка FileReader, имя временного файла или само имя файла."""
    if not name or not str(name).strip():
        return True
    s = str(name).strip()
    if s.startswith(UNSUPPORTED_PREFIXES) or TMP_RE.search(s):
        return True
    return bool(filename) and s.lower() == re.split(r"[\\/]", filename)[-1].lower()


def archive_suffix(text: str) -> str:
    """«(архив XML, HTML и файлов электронной подписи)» по именам вложений «=== имя ===» в тексте архива."""
    exts = [re.search(r"\.[a-z0-9]{2,5}$", n.strip().lower()) for n in ARCHIVE_ENTRY_RE.findall(text or "")]
    exts = {m.group(0) for m in exts if m}
    labels = [label for group, label in ARCHIVE_LABELS if exts & set(group)]
    if not labels:
        return " (архив)"
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " и " + labels[-1]
    return f" (архив {joined})"


def format_suffix(source_format: Optional[str], filename: str, text: str, name: str) -> str:
    """Пометка о формате файла, если её ещё нет в названии.

    Args:
        source_format: Настоящий формат файла (``.xml``, ``.html``, ``.zip`` …), определённый по содержимому.
        filename: Имя файла (если формат не передан — по расширению имени).
        text: Текст документа (признаки ЕИС, состав архива).
        name: Текущее название.

    Returns:
        str: Пометка (с ведущим пробелом или запятой) или пустая строка.
    """
    fmt = (source_format or "").lower()
    if not fmt:
        m = re.search(r"\.([a-z0-9]{2,5})$", (filename or "").lower())
        fmt = f".{m.group(1)}" if m else ""
    low = name.lower()
    if fmt == ".xml" and "xml" not in low:
        return " (формат XML)"
    if fmt in (".html", ".htm") and "печатн" not in low:
        eis = EIS_RE.search(text[:20000] if text else "") or (filename or "").lower().startswith("печатная")
        return ", печатная форма ЕИС" if eis else " (печатная форма)"
    if fmt in (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz") and "архив" not in low:
        return archive_suffix(text)
    return ""


DATE_RX = r"(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})"
NUM_RX = r"([A-Za-zА-ЯЁа-яё0-9][\wА-ЯЁа-яё\-/.()]{0,40}?)"
# Унифицированные формы (ТОРГ-12, УПД, КС-2/КС-3): «Номер документа | Дата составления» и под ними значения
FORM_REQ_RE = re.compile(r"Номер\s+документа\W{1,40}Дата\s+составления\W{1,60}?([\wА-ЯЁа-яё\-/]{1,30})\W{1,20}?"
                         + DATE_RX, re.I)
CONTRACT_WORD_RE = re.compile(r"\b(контракт|договор)", re.I)
# Документы, у которых свои номер и дата (не договора): им номер договора не подставляется.
# «Сведения о заключенном контракте» (печатная форма ЕИС) — подставляется: это сведения о самом договоре
NOT_CONTRACT_RE = re.compile(r"\b(акт|накладн|счет|счёт|фактур|протокол|сведени\w*\s+об\s+исполнени|соглашени|"
                             r"поручени|сертификат|удостоверени|спецификац|по\s+(?:договору|контракту))", re.I)


def _iso_to_ru(value: str) -> str:
    """«2026-03-23» → «23.03.2026» (прочее — как есть)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else value


def _has_date(name: str) -> bool:
    """В названии есть дата ДД.ММ.ГГГГ."""
    return bool(re.search(r"\d{2}\.\d{2}\.\d{4}", name))


def _has_number(name: str) -> bool:
    """В названии есть «№ …»."""
    return "№" in name


def requisites_from_text(name: str, text: str) -> tuple:
    """Номер и дата самого документа из текста: рядом с заголовком или в шапке унифицированной формы.

    * «ТОВАРНАЯ НАКЛАДНАЯ № 5486 от 24.03.2026» — по первым словам названия;
    * «Номер документа | Дата составления | 5486 | 24.03.2026» (ТОРГ-12, УПД, КС-2, КС-3).

    Args:
        name: Название документа (по первым словам ищется заголовок).
        text: Текст документа.

    Returns:
        tuple: (номер или None, дата «ДД.ММ.ГГГГ» или None).
    """
    if not text:
        return None, None
    head = text[:30000]
    words = re.findall(r"[А-ЯЁа-яёA-Za-z\-]{3,}", name or "")[:2]
    if words:
        title = r"\s+".join(re.escape(w) for w in words)
        for rx in (rf"{title}[^\n]{{0,80}}?№\s*{NUM_RX}\s+от\s+{DATE_RX}",
                   rf"{title}[^\n]{{0,80}}?от\s+{DATE_RX}\s*(?:г\.?\s*)?№\s*{NUM_RX}(?=[\s,;.]|$)"):
            m = re.search(rx, head, re.I)
            if m:
                a, b = m.group(1), m.group(2)
                number, date = (a, b) if "№" in m.group(0).split("от")[0] else (b, a)
                return number.rstrip(".,;"), _iso_to_ru(date)
    m = FORM_REQ_RE.search(head)
    if m:
        return m.group(1), _iso_to_ru(m.group(2))
    return None, None


def contract_requisites(raw: Dict[str, Any]) -> tuple:
    """Номер и дата заключения договора из извлечённых данных (``contract_number``, дата вида ``signing``)."""
    cn = raw.get("contract_number") if isinstance(raw, dict) else None
    number = cn.get("value") if isinstance(cn, dict) else None
    date = None
    for d in (raw.get("dates") or []) if isinstance(raw, dict) else []:
        if isinstance(d, dict) and d.get("kind") == "signing" and d.get("value"):
            date = _iso_to_ru(str(d["value"])[:10])
            break
    if date is None:
        for d in (raw.get("dates") or []) if isinstance(raw, dict) else []:
            ctx = str(d.get("context") or "").lower() if isinstance(d, dict) else ""
            if isinstance(d, dict) and d.get("value") and re.search(r"заключени|подписани\w* (договор|контракт)", ctx):
                date = _iso_to_ru(str(d["value"])[:10])
                break
    return (str(number).strip() if number else None), date


def add_requisites(name: str, raw: Dict[str, Any], text: str) -> str:
    """Дописывает к названию «от ДД.ММ.ГГГГ № номер», если их нет, а в документе они есть.

    Сначала — реквизиты самого документа из текста (заголовок, шапка формы); для самого договора/контракта
    (электронный контракт в XML, печатная форма) — номер договора и дата заключения из извлечённых данных.

    Args:
        name: Название после :func:`normalize_name`.
        raw: ``raw_data`` документа.
        text: Текст документа.

    Returns:
        str: Название с реквизитами (или прежнее, если реквизитов не нашлось).
    """
    if _has_date(name) and _has_number(name):
        return name
    number, date = requisites_from_text(name, text)
    if not (number or date) and CONTRACT_WORD_RE.search(name) and not NOT_CONTRACT_RE.search(name):
        number, date = contract_requisites(raw)
    tail = ""
    if date and not _has_date(name):
        tail += f" от {date}"
    if number and not _has_number(name):
        tail += f" № {number}"
    if not tail:
        return name
    # реквизиты — сразу после вида документа, до уточнений в скобках / через запятую
    m = re.search(r"\s*[(,]", name)
    return (name[:m.start()] + tail + name[m.start():]) if m else name + tail


def finalize_document_name(raw: Dict[str, Any], filename: str, source_format: Optional[str] = None,
                           text: str = "") -> Optional[str]:
    """Доводит ``raw_data.document_name.value`` до единого вида (на месте) и возвращает прежнее значение.

    Args:
        raw: ``raw_data`` документа.
        filename: Имя файла.
        source_format: Настоящий формат файла (по содержимому), если известен.
        text: Текст документа.

    Returns:
        str | None: Название до правок (для журнала); ``None``, если ``raw`` не словарь.
    """
    if not isinstance(raw, dict):
        return None
    dn = raw.get("document_name")
    if not isinstance(dn, dict):
        dn = {"value": dn if isinstance(dn, str) else None, "confidence": 0.0}
        raw["document_name"] = dn
    before = dn.get("value")
    name = before
    if is_unsupported_text(text) or is_weak_name(name, filename) or FILE_EXT_RE.search(str(name or "")):
        # служебная строка, имя временного файла или имя вложения («….docx») — название из имени файла
        name = name_from_filename(filename) or str(name or "")
        dn["confidence"] = min(float(dn.get("confidence") or 0.5), 0.5)
    name = normalize_name(name)
    if not is_unsupported_text(text):
        # электронный контракт, товарная накладная и т. п. — с реквизитами «от ДД.ММ.ГГГГ № номер»
        name = add_requisites(name, raw, text)
    if is_unsupported_text(text):
        name += " (формат файла не поддерживается)"
    else:
        name += format_suffix(source_format, filename, text, name)
    dn["value"] = name
    return before

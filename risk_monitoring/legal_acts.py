"""Нормативные акты (44-ФЗ, 223-ФЗ, НК РФ, постановления Правительства): разбор текста по структуре.

Тексты — сводные редакции (КонсультантПлюс, PDF / HTML, присланы 10.11.2026). Через API ЕИС нормативные акты
не выгружаются, поэтому новая редакция загружается вручную: каждая загрузка — версия акта с датой редакции.

Структура (общие уровни ``lvl1 … lvl4``):

=====================  ========================  ====================  ==================  ============
вид акта               lvl1                      lvl2 («1.», «1.1.»)   lvl3 («1)»)         lvl4 («а)»)
=====================  ========================  ====================  ==================  ============
закон (``law``)        статья                    часть                 пункт               подпункт
кодекс (``code``)      статья                    пункт                 подпункт            абзац «а)»
постановление          —                         пункт                 подпункт «1)»       подпункт «а)»
=====================  ========================  ====================  ==================  ============

У постановления, кроме основного текста, есть утверждённые им документы («Утверждены постановлением …
ПРАВИЛА …», «Приложение N 1») — это сегменты (``segment``): пункт 3 Правил и пункт 3 самого постановления — разные
нормы.

Редакционные пометки КонсультантПлюс («(в ред. Федерального закона …)», «КонсультантПлюс: примечание.»),
номера страниц и навигация сайтов в текст норм не попадают.

Разбор — :func:`parse_act`; адрес нормы для человека — :func:`address`; ключ для проверки ссылок — ``key``
(``(lvl1, lvl2, lvl3, lvl4)``, пустые уровни — ``None``).
"""
import hashlib
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from configs.logger import get_logger

logger = get_logger(__name__)

# --- распознавание акта ----------------------------------------------------------------------------------------

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
          "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}

# «5 апреля 2013 года\nN 44-ФЗ» (PDF КонсультантПлюс) и «5 августа 2000 г. N 117-ФЗ» (RTF КонсультантПлюс)
LAW_HEAD_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\s+(?:года|г\.)\s*\n?\s*N\s*([\d\-]+-ФЗ)",
                         re.I)
# шапка в веб-версиях (ГАРАНТ, печать страницы из браузера): «Федеральный закон от 28 ноября 2025 г. № 426-ФЗ "О …"»
LAW_INLINE_RE = re.compile(r"Федеральный\s+закон\s+от\s+(\d{1,2})\s+(" + "|".join(MONTHS) +
                           r")\s+(\d{4})\s*г\.?\s*(?:№|N)\s*([\d\-]+-ФЗ)(?:\s*\"([^\"\n]{5,300})\")?", re.I)
# то же с датой цифрами (government.ru): «Федеральный закон от 28.11.2025 г. № 426-ФЗ»
LAW_INLINE_NUM_RE = re.compile(r"Федеральный\s+закон\s+от\s+(\d{2})\.(\d{2})\.(\d{4})\s*г?\.?\s*(?:№|N)\s*([\d\-]+-ФЗ)"
                               r"(?:\s*\"([^\"\n]{5,300})\")?", re.I)
# служебные строки печати веб-страницы: «10.11.2026, 15:47», адрес страницы, «1/39»
PRINT_NOISE_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4},\s*\d{1,2}:\d{2}|https?://\S+|\d{1,4}/\d{1,4}|Узнать больше|Реклама)$"
                            r"|\|\s*Документы\b")
WEB_PRINT_MIN = 3   # столько служебных строк печати — источник «печать веб-страницы» (в тексте может быть реклама)
DECREE_HEAD_RE = re.compile(r"ПОСТАНОВЛЕНИЕ\s*\n\s*от\s+(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\s+г\.\s+N\s*(\d+)",
                            re.I)
EDITION_RE = re.compile(r"ред\.?\s*от\s*(\d{2})[.\-_](\d{2})[.\-_](\d{4})"
                        r"|(?:действующая\s+)?редакци[яи]\s*\(с\s*(\d{2})\.(\d{2})\.(\d{4})\)", re.I)
# дата изменяющего акта в перечне «(в ред. …)»: федеральный закон или постановление («от 04.08.2026 N 293-ФЗ»,
# «от 29.09.2026 N 1255»); постановления Конституционного Суда («N 2-П», «N 257-О») редакцию не меняют
AMEND_DATE_RE = re.compile(r"от\s+(\d{2})\.(\d{2})\.(\d{4})\s+N\s*\d+(?:-ФЗ)?(?![\d\-]*-[ПО]\b)")

# известные акты: (номер, дата принятия) → ключ и вид. Номер федерального закона уникален только в пределах года:
# «223-ФЗ» от 18.07.2011 — закупки, от 13.07.2015 — СРО финансового рынка.
KNOWN = {
    ("44-ФЗ", "2013-04-05"): ("44-ФЗ", "law"),
    ("223-ФЗ", "2011-07-18"): ("223-ФЗ", "law"),
    ("135-ФЗ", "2006-07-26"): ("135-ФЗ", "law"),
    ("117-ФЗ", "2000-08-05"): ("НК", "code"),       # часть вторая (ст. 164 — ставки НДС)
    ("146-ФЗ", "1998-07-31"): ("НК-1", "code"),     # часть первая
    ("145-ФЗ", "1998-07-31"): ("БК", "code"),
    ("426-ФЗ", "2025-11-28"): ("ФБ-2026", "law"),
    ("149-ФЗ", "2006-07-27"): ("149-ФЗ", "law"),    # об информации, информационных технологиях и о защите информации   # о федеральном бюджете на 2026 год и плановый период 2027–2028
}
CODE_TITLE_RE = re.compile(r"^\s*([А-ЯЁ]+\s+)?КОДЕКС\b", re.M)

# --- разбор строк ----------------------------------------------------------------------------------------------

ARTICLE_RE = re.compile(r"^Статья\s+(\d+(?:\.\d+)*(?:-\d+)?)\.\s*(.*)$")
CHAPTER_RE = re.compile(r"^(Глава|Раздел|ГЛАВА|РАЗДЕЛ)\s+[\dIVXLC]+\.?\s")
SECTION_RE = re.compile(r"^([IVXLC]{1,6})\.\s+\S")
LVL2_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,2})\.\s+(?=\S)")
LVL3_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,2})\)\s+(?=\S)")
LVL4_RE = re.compile(r"^([а-я](?:\(\d+\))?)\)\s+(?=\S)")
APPROVED_RE = re.compile(r"^(Утвержден[аоы]?|УТВЕРЖДЕН[АОЫ]?)\s*$")
APPENDIX_RE = re.compile(r"^Приложение\s+(N\s*\d+|№\s*\d+)?\s*$|^Приложение\s+N\s*\d+\b")
# примечание КонсультантПлюс в RTF — отдельный абзац без заголовка «КонсультантПлюс: примечание.»:
# «Положения пункта 3 статьи 164 (в редакции Федерального закона от …) применяются …»
RTF_NOTE_RE = re.compile(r"^(Положени[ея]|Действие|Норм[аы]|Пункт|Подпункт|Абзац|Стать[яи]|Часть|Требовани[ея])\b"
                         r"(?=[^\n]{0,250}?\bстать[ией]\s+\d)(?=[^\n]*Федеральн\w+\s+закон)[^\n]{0,250}?"
                         r"\b(применя\w*|распространя\w*|вступа\w*|не\s+применя\w*)\b", re.I)
# пометка в конце строки или внутри неё (RTF): «… 22 процента … (в ред. Федеральных законов от …, от …)»
INLINE_EDITORIAL_RE = re.compile(
    r"\s*\((?:в ред\.|в редакции|введен[аоы]?\s|абзац введен|пп?\.\s*[\d.]+\s+(?:в ред\.|введен))"
    r"[^()]*(?:\([^()]*\)[^()]*)*\)")
EDITORIAL_RE = re.compile(
    r"^\((в ред\.|п\.|пп\.|часть|ч\.|абзац|статья|подп\.|введен|утратил|наименование|в редакции|ред\.)", re.I)
NOTE_RE = re.compile(r"^КонсультантПлюс: примечание\.?$")
PAGE_RE = re.compile(r"^\d{1,4}$")
LOST_FORCE_RE = re.compile(r"^(утратил|утратила|утратило|утратили)\s+силу", re.I)

SITE_NOISE = {"Главная", "Найти", "Войти", "Зарегистрироваться", "Серия", "СПРАВКА", "РЕДАКЦИЯ", "Содержание"}


def _ru_date(d: str, m: str, y: str) -> str:
    """«5 апреля 2013» → ``2013-04-05``."""
    return date(int(y), MONTHS[m.lower()], int(d)).isoformat()


def identify(text: str, file_name: Optional[str] = None, edition_date: Optional[str] = None) -> Dict[str, Any]:
    """Вид, номер, дата и название акта и дата редакции.

    Дата редакции — переданная явно (``edition_source = given``), из имени файла («ред. от 04.08.2026») или текста
    (``stated``); если её нет — самая поздняя дата изменяющего документа из «Списка изменяющих документов»
    (``amendments``), иначе дата принятия (``act_date``). В выгрузках КонсультантПлюс без шапки даты редакции в
    тексте нет — её нужно передавать при загрузке.

    Args:
        text: Текст акта.
        file_name: Имя файла.
        edition_date: Дата редакции ``YYYY-MM-DD``.

    Returns:
        Dict[str, Any]: ``kind`` (law / code / decree), ``number``, ``act_date``, ``title``, ``act_key``,
        ``edition_date``, ``edition_source``.

    Raises:
        ValueError: Акт не распознан.
    """
    head = text[:6000]
    info: Dict[str, Any] = {}
    m = LAW_HEAD_RE.search(head) or LAW_INLINE_RE.search(head) or LAW_INLINE_NUM_RE.search(head)
    if m:
        number = m.group(4).upper()
        act_date = (f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m.re is LAW_INLINE_NUM_RE
                    else _ru_date(*m.group(1, 2, 3)))
        key, kind = KNOWN.get((number, act_date), (f"{number} от {act_date}", "law"))
        if CODE_TITLE_RE.search(head):     # «НАЛОГОВЫЙ КОДЕКС» прописными в заголовке, не «…кодексом» в тексте
            kind = "code"
        info = {"kind": kind, "number": number, "act_date": act_date, "act_key": key}
    else:
        m = DECREE_HEAD_RE.search(head)
        if not m:
            raise ValueError("Не распознан вид акта: нет «N …-ФЗ» и «ПОСТАНОВЛЕНИЕ от … N …» в начале текста")
        info = {"kind": "decree", "number": m.group(4), "act_date": _ru_date(*m.group(1, 2, 3)),
                "act_key": f"ПП-{m.group(4)}"}
    title_m = re.search(r"(ФЕДЕРАЛЬНЫЙ ЗАКОН|КОДЕКС[^\n]*|ПОСТАНОВЛЕНИЕ[^\n]*\n[^\n]*N\s*\d+)\s*\n((?:[А-ЯЁ0-9 ,\-\"«»().]+\n){1,8})",
                        head)
    if title_m:
        info["title"] = re.sub(r"\s+", " ", title_m.group(2)).strip().capitalize()[:500]
    if m and m.re in (LAW_INLINE_RE, LAW_INLINE_NUM_RE) and m.group(5):
        info["title"] = m.group(5).strip()
    if not info.get("title"):
        # «ФЕДЕРАЛЬНЫЙ ЗАКОН\nО федеральном бюджете …» строчными (government.ru)
        t_m = re.search(r"ФЕДЕРАЛЬНЫЙ ЗАКОН\s*\n\s*(О\s[^\n]{5,300})", head)
        if t_m:
            info["title"] = t_m.group(1).strip()
    code_m = CODE_TITLE_RE.search(head) if info["kind"] == "code" else None
    if code_m and (not info.get("title") or info["title"].lower().startswith("часть")):
        # «НАЛОГОВЫЙ КОДЕКС РОССИЙСКОЙ ФЕДЕРАЦИИ» + «ЧАСТЬ ВТОРАЯ»
        line = [x.strip() for x in head[code_m.start():].split("\n")[:6] if x.strip()]
        part = line[1] if len(line) > 1 and line[1].upper().startswith("ЧАСТЬ") else ""
        info["title"] = (line[0].strip() + (". " + part if part else "")).capitalize()[:500]
    # «ред. от …» в тексте — только в шапке до перечня изменяющих законов: внутри перечня бывает «N 179-ФЗ (ред. от
    # 31.12.2002)» — это редакция изменяющего закона, а не акта
    cut = min([i for i in (head.find("(в ред."), head.find("Список изменяющих")) if i >= 0] or [len(head)])
    ed = EDITION_RE.search(file_name or "") or EDITION_RE.search(head[:cut])
    if edition_date:
        info["edition_date"], info["edition_source"] = date.fromisoformat(edition_date).isoformat(), "given"
    elif ed:
        d, mo, y = (ed.group(1), ed.group(2), ed.group(3)) if ed.group(1) else (ed.group(4), ed.group(5), ed.group(6))
        info["edition_date"], info["edition_source"] = f"{y}-{mo}-{d}", "stated"
    else:
        block = head.split("Список изменяющих документов", 1)
        # перечень — до первой статьи / раздела / главы (у НК ч. 1 — больше 5 тыс. символов)
        amend = re.split(r"\n\s*(?:Статья\s+\d|Раздел\s+[IVXLC\d]|Глава\s+\d)", block[1], maxsplit=1)[0] if len(block) > 1 else ""
        if len(block) > 1 and len(amend) == len(block[1]):
            amend = (text.split("Список изменяющих документов", 1)[1])[:30000]
            amend = re.split(r"\n\s*(?:Статья\s+\d|Раздел\s+[IVXLC\d]|Глава\s+\d)", amend, maxsplit=1)[0]
        dates = [f"{y}-{mo}-{d}" for d, mo, y in AMEND_DATE_RE.findall(amend)]
        if not dates:
            # RTF: перечень изменяющих законов — абзац «(в ред. Федеральных законов от … )» под заголовком (у НК
            # ч. 2 — 18 тыс. символов)
            first_article = text.find("Статья ")
            m_ed = re.search(r"\(в ред\.[^()]*(?:\([^()]*\)[^()]*)*\)", text[:60000])
            if m_ed and m_ed.start() < 3000 and (first_article == -1 or m_ed.start() < first_article):
                dates = [f"{y}-{mo}-{d}" for d, mo, y in AMEND_DATE_RE.findall(m_ed.group(0))]
        info["edition_date"] = max(dates) if dates else info["act_date"]
        info["edition_source"] = "amendments" if dates else "act_date"
    return info


def clean_lines(text: str) -> List[str]:
    """Строки текста без редакционных пометок, примечаний, номеров страниц и навигации сайта.

    Примечание КонсультантПлюс («КонсультантПлюс: примечание.» и 1–4 строки текста) заканчивается строкой с точкой
    в конце или началом структурной единицы; пометка «(в ред. …» на нескольких строках — строкой с «)» в конце. Пустые строки в выгрузке PDF — разрывы страниц, а не конец абзаца.
    """
    # колонтитул печати страницы: строка сразу после времени печати («10.11.2026, 16:11» → «Документы - Правительство
    # России») повторяется на каждой странице
    raw_lines = [x.strip() for x in text.replace("\f", "\n").splitlines()]
    nonblank = [x for x in raw_lines if x]
    print_headers = {nonblank[i + 1] for i, x in enumerate(nonblank[:-1])
                     if re.match(r"^\d{2}\.\d{2}\.\d{4},\s*\d{1,2}:\d{2}$", x)}
    out: List[str] = []
    note = 0                     # > 0 — идёт примечание (сколько строк уже пропущено)
    editorial = 0                # > 0 — продолжение пометки «(в ред. …» на следующих строках
    for raw in text.replace("\f", "\n").splitlines():
        s = raw.strip()
        if not s:
            continue
        if editorial:
            editorial = 0 if (s.endswith(")") or editorial >= 3) else editorial + 1
            continue
        if EDITORIAL_RE.match(s) and not s.endswith(")"):
            editorial = 1
            continue
        if NOTE_RE.match(s):
            note = 1
            continue
        if note:
            if not (ARTICLE_RE.match(s) or LVL2_RE.match(s) or LVL3_RE.match(s) or LVL4_RE.match(s)):
                note = 0 if (s.endswith(".") or note >= 4) else note + 1
                continue
            note = 0
        if (PAGE_RE.match(s) or s in SITE_NOISE or EDITORIAL_RE.match(s) or PRINT_NOISE_RE.search(s)
                or s in print_headers or re.match(r"^[\w.\-]+\.ru(/\S*)?$", s)):
            continue
        if RTF_NOTE_RE.match(s):
            continue
        s = INLINE_EDITORIAL_RE.sub("", s).strip()
        if s:
            out.append(s)
    return out


APPENDIX_START_RE = re.compile(r"^Приложение\s+(?:N\s*|№\s*)?\d+(?:\.|\s*$|\s+к\s|\s+[А-ЯЁ])")


def _drop_toc(lines: List[str]) -> List[str]:
    """Убирает оглавление веб-версии: подряд идущие (3 и больше) заголовки «Статья N. …», каждый из которых ещё раз
    встречается дальше в тексте (подряд идущие утратившие силу статьи — не оглавление: они не повторяются)."""
    out: List[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and ARTICLE_RE.match(lines[j]):
            j += 1
        if j - i >= 3:
            rest = set(lines[j:])
            if all(lines[k] in rest for k in range(i, j)):
                i = j
                continue
        out.append(lines[i])
        i += 1
    return out


# подпись в конце закона: «Президент Российской Федерации В.ПУТИН» (одной строкой или «Президент» отдельно)
SIGNATURE_RE = re.compile(r"^(Президент(\s+Российской\s+Федерации)?|Председатель\s+Правительства)(\s+[А-ЯЁ]\.\s*[А-ЯЁ]+)?$")


def _segments(lines: List[str], kind: str) -> List[Tuple[Optional[str], List[str]]]:
    """Основной текст и утверждённые документы / приложения (для постановлений)."""
    if kind != "decree":
        return [(None, lines)]
    segs: List[Tuple[Optional[str], List[str]]] = [(None, [])]
    i = 0
    while i < len(lines):
        s = lines[i]
        if APPROVED_RE.match(s) or APPENDIX_RE.match(s):
            j = i + 1
            # «постановлением Правительства … от … N …» — пропускаем до заголовка прописными
            while j < len(lines) and not re.match(r"^[А-ЯЁ][А-ЯЁ ,\-«»\"]{3,}$", lines[j]) and j - i < 8:
                j += 1
            title = [lines[j]] if j < len(lines) else [s]
            i = j + 1
            # продолжение заголовка прописными
            while i < len(lines) and re.match(r"^[А-ЯЁ0-9][А-ЯЁ0-9 ,\-«»\"().]{3,}$", lines[i]):
                title.append(lines[i])
                i += 1
            name = s if APPENDIX_RE.match(s) else " ".join(title)
            segs.append((_segment_name(name), []))
            continue
        segs[-1][1].append(s)
        i += 1
    return [s for s in segs if s[1]]


def _segment_name(title: str) -> str:
    """Название сегмента: «ПРАВИЛА ОПРЕДЕЛЕНИЯ РАЗМЕРА ШТРАФА» → «Правила определения размера штрафа»."""
    t = re.sub(r"\s+", " ", title.strip()).rstrip(",")
    if t.isupper():
        t = t.capitalize()
    return t[:200]


SKIP_SEGMENTS = ("изменения",)   # «Изменения, которые вносятся в акты» — нормы других актов


def parse_act(text: str, file_name: Optional[str] = None, edition_date: Optional[str] = None) -> Dict[str, Any]:
    """Разбор акта на нормы.

    Args:
        text: Текст акта (из PDF или HTML).
        file_name: Имя файла (дата редакции).
        edition_date: Дата редакции ``YYYY-MM-DD`` (приоритетнее имени файла).

    Returns:
        Dict[str, Any]: реквизиты :func:`identify`, ``norms`` — список норм (``segment``, ``lvl1…lvl4``,
        ``heading`` — название статьи или раздела, ``text``, ``address``, ``lost_force``), ``text_sha256``.
    """
    info = identify(text, file_name, edition_date)
    kind = info["kind"]
    lines = clean_lines(text)
    # текст до первой статьи (шапка, преамбула) у законов не нормы
    if kind in ("law", "code"):
        lines = _drop_toc(lines)
        first = next((i for i, s in enumerate(lines) if ARTICLE_RE.match(s)), len(lines))
        lines = lines[first:]
        # приложения закона (таблицы закона о бюджете) — не нормы: разбор до первого «Приложение N …» после статей
        end = next((i for i, s in enumerate(lines) if APPENDIX_START_RE.match(s) or SIGNATURE_RE.match(s)), len(lines))
        lines = lines[:end]
    norms: List[Dict[str, Any]] = []
    for segment, seg_lines in _segments(lines, kind):
        if segment and segment.lower().startswith(SKIP_SEGMENTS):
            continue
        norms.extend(_parse_segment(seg_lines, kind, segment, info["act_key"]))
    info["norms"] = norms
    # печать страницы сайта (ГАРАНТ и т. п.): между строками закона — реклама, она попадает в текст норм и даже
    # разрывает числа («не превышающего Kia4,0 процента»); такой источник лучше заменить PDF сводной редакции
    if sum(1 for s in text.splitlines() if PRINT_NOISE_RE.search(s.strip())) >= WEB_PRINT_MIN:
        info["warnings"] = ["Источник — печать веб-страницы: проверьте, что текст напечатан полностью (браузер "
                            "печатает только видимую часть) и без вставок сайта (реклама); надёжнее RTF / PDF "
                            "сводной редакции"]
    info["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return info


def _parse_segment(lines: Iterable[str], kind: str, segment: Optional[str], act_key: str) -> List[Dict[str, Any]]:
    """Нормы одного сегмента: каждая строка — новая единица или продолжение текущей."""
    norms: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    lvl1 = lvl2 = lvl3 = None
    heading = None

    def start(l1, l2, l3, l4, first_text):
        """Новая единица текста."""
        nonlocal cur
        cur = {"segment": segment, "lvl1": l1, "lvl2": l2, "lvl3": l3, "lvl4": l4, "heading": heading,
               "lines": [first_text]}
        norms.append(cur)

    for s in lines:
        m = ARTICLE_RE.match(s)
        if m and kind in ("law", "code"):
            lvl1, lvl2, lvl3 = m.group(1), None, None
            heading = m.group(2).strip() or None
            start(lvl1, None, None, None, s)
            continue
        if CHAPTER_RE.match(s) and kind in ("law", "code"):
            continue
        if SECTION_RE.match(s) and kind == "decree":
            heading = s
            continue
        m2, m3, m4 = LVL2_RE.match(s), LVL3_RE.match(s), LVL4_RE.match(s)
        if m2 and (kind == "decree" or lvl1 is not None):
            lvl2, lvl3 = m2.group(1), None
            start(lvl1, lvl2, None, None, s)
        elif m3 and (kind == "decree" or lvl1 is not None):
            lvl3 = m3.group(1)
            start(lvl1, lvl2, lvl3, None, s)
        elif m4 and (kind == "decree" or lvl1 is not None):
            start(lvl1, lvl2, lvl3, m4.group(1), s)
        elif cur is not None:
            cur["lines"].append(s)
        elif kind == "decree":
            start(None, None, None, None, s)   # вводный текст до первого пункта
    out = []
    for n in norms:
        txt = _join(n.pop("lines"))
        n["text"] = txt
        n["lost_force"] = bool(LOST_FORCE_RE.match(re.sub(r"^[\dа-я.)\s]+", "", txt.split("Статья", 1)[-1]).strip()))
        n["address"] = address(n, kind, act_key)
        out.append(n)
    return out


def _join(lines: List[str]) -> str:
    """Склейка строк, разорванных при вёрстке PDF (перенос «-» в конце строки — без пробела)."""
    out = ""
    for s in lines:
        if not out:
            out = s
        elif out.endswith("-") and not out.endswith(" -"):
            out = out[:-1] + s
        else:
            out += " " + s
    return re.sub(r"\s+", " ", out).strip()


LEVEL_NAMES = {
    "law": ("ст.", "ч.", "п.", "пп."),
    "code": ("ст.", "п.", "пп.", "абз."),
    "decree": (None, "п.", "пп.", "пп."),
}


def address(n: Dict[str, Any], kind: str, act_key: str) -> str:
    """Адрес нормы: «п. 1 ч. 1 ст. 33 44-ФЗ», «пп. "а" п. 3 Правил ПП-1042», «п. 3 ст. 164 НК»."""
    names = LEVEL_NAMES.get(kind, LEVEL_NAMES["law"])
    parts = []
    for lvl, name in zip(("lvl4", "lvl3", "lvl2", "lvl1"), reversed(names)):
        v = n.get(lvl)
        if v is None or name is None:
            continue
        parts.append(f'{name} "{v}"' if lvl == "lvl4" and not v[0].isdigit() else f"{name} {v}")
    seg = n.get("segment")
    if seg:
        parts.append(_genitive(seg))
    parts.append(act_key)
    return " ".join(parts)


def _genitive(segment: str) -> str:
    """«Правила» → «Правил», «Положение» → «Положения», «Приложение N 1» → «приложения N 1»."""
    low = segment.lower()
    if low.startswith("правила"):
        return "Правил"
    if low.startswith("положение"):
        return "Положения"
    if low.startswith("перечень"):
        return "Перечня"
    if low.startswith("приложение"):
        return "приложения" + segment[len("Приложение"):]
    return segment


def norm_key(n: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Ключ нормы для проверки ссылок (без сегмента: модель редко называет сегмент постановления точно)."""
    return n.get("lvl1"), n.get("lvl2"), n.get("lvl3"), n.get("lvl4")


def chunks_for_embedding(norms: List[Dict[str, Any]], max_chars: int = 2500) -> List[Dict[str, Any]]:
    """Куски для эмбеддингов: часть (пункт постановления) вместе с её пунктами и подпунктами; длинные — делятся.

    Returns:
        List[Dict[str, Any]]: ``address`` (часть), ``text`` (с адресом и названием статьи в начале).
    """
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    order: List[Tuple] = []
    for n in norms:
        if n["lost_force"]:
            continue
        g = (n.get("segment"), n.get("lvl1"), n.get("lvl2"))
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(n)
    out = []
    for g in order:
        items = groups[g]
        head = items[0]
        base = {"segment": head.get("segment"), "lvl1": head.get("lvl1"), "lvl2": head.get("lvl2")}
        addr = items[0]["address"] if items[0].get("lvl3") is None else items[0]["address"]
        prefix = f"{addr}" + (f" ({head['heading']})" if head.get("heading") else "") + ": "
        body = " ".join(i["text"] for i in items)
        for k in range(0, len(body), max_chars):
            out.append({**base, "address": addr, "text": prefix + body[k:k + max_chars]})
    return out


# --- чтение файла ----------------------------------------------------------------------------------------------

HTML_BLOCK_TAGS = ["p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section",
                   "article", "pre", "blockquote", "dd", "dt"]


def html_to_text(raw: bytes) -> str:
    """Текст HTML-страницы акта (docs.cntd.ru, consultant.ru, tks.ru): без скриптов, меню и стилей, абзацы — строками.

    Строки разделяются только на блочных элементах и ``<br>``: ссылки внутри абзаца («в пунктах <a>1</a> и <a>2</a>»)
    остаются в одной строке.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(HTML_BLOCK_TAGS):
        tag.append("\n")
    lines = [re.sub(r"\s+", " ", s).strip() for s in soup.get_text("").splitlines()]
    return "\n".join(s for s in lines if s)


RTF_TOKEN_RE = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+",
                          re.S)
RTF_SKIP_DEST = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "fldinst", "header", "footer", "object",
                 "themedata", "datastore", "xmlnstbl", "listtable", "listoverridetable", "rsidtbl", "generator"}


def rtf_to_text(raw: bytes) -> str:
    """Текст RTF (выгрузка КонсультантПлюс «Сохранить в RTF»): ``\\uN`` и ``\\'hh`` (cp1251), абзацы ``\\par``.

    Служебные группы (таблица шрифтов, стили, ``\\fldinst`` гиперссылок, ``{\\* …}``) пропускаются; текст ссылок
    (``\\fldrslt``) остаётся.
    """
    data = raw.decode("latin-1")
    out: List[str] = []
    stack: List[Tuple[bool, int]] = []      # (пропускать группу, uc) для каждой открытой группы
    skip, uc, pending_skip = False, 1, 0
    byte_buf = bytearray()

    def flush():
        """Накопленные байты \\'hh → текст cp1251."""
        if byte_buf:
            if not skip:
                out.append(byte_buf.decode("cp1251", "replace"))
            byte_buf.clear()

    first_in_group = False
    for m in RTF_TOKEN_RE.finditer(data):
        word, arg, hexb, sym, brace, plain = m.groups()
        if hexb is None:
            flush()
        if brace == "{":
            stack.append((skip, uc))
            first_in_group = True
            continue
        if brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
            first_in_group = False
            continue
        if word is not None:
            if first_in_group and word in RTF_SKIP_DEST:
                skip = True
            elif word == "u" and arg is not None:
                n = int(arg)
                if not skip:
                    out.append(chr(n + 65536 if n < 0 else n))
                pending_skip = uc            # символы-замены после \uN
            elif word == "uc" and arg is not None:
                uc = int(arg)
            elif word in ("par", "line", "sect", "page", "row") and not skip:
                out.append("\n")
            elif word in ("tab", "cell") and not skip:
                out.append("\t")
            first_in_group = False
            continue
        if sym is not None:
            if sym == "*" and first_in_group:
                skip = True
            elif sym in "\\{}" and not skip:
                out.append(sym)
            elif sym == "~" and not skip:
                out.append(" ")
            first_in_group = False
            continue
        if hexb is not None:
            if pending_skip:
                pending_skip -= 1
            else:
                byte_buf.append(int(hexb, 16))
            continue
        if plain is not None:
            if pending_skip:
                cut = min(pending_skip, len(plain))
                plain, pending_skip = plain[cut:], pending_skip - cut
            if plain and not skip:
                out.append(plain)
            first_in_group = False
    flush()
    return re.sub(r"[ \t]+\n", "\n", "".join(out))


def read_source(path: str, file_name: Optional[str] = None) -> str:
    """Текст акта из файла: HTML — :func:`html_to_text`, PDF — ``pdftotext`` (если нет — ``PyPDF2``), RTF —
    :func:`rtf_to_text`, TXT — как есть.

    Args:
        path: Путь к файлу.
        file_name: Исходное имя (расширение).

    Returns:
        str: Текст.

    Raises:
        ValueError: Формат не поддерживается или текст пуст.
    """
    import os
    import shutil
    import subprocess

    ext = os.path.splitext(file_name or path)[1].lower()
    with open(path, "rb") as fh:
        raw = fh.read()
    if ext in (".html", ".htm") or raw[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        text = html_to_text(raw)
    elif ext == ".pdf" or raw[:5] == b"%PDF-":
        if shutil.which("pdftotext"):
            text = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"], capture_output=True, timeout=180,
                                  check=True).stdout.decode("utf-8", "replace")
        else:
            from PyPDF2 import PdfReader

            text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    elif ext == ".rtf" or raw[:5] == b"{\\rtf":
        text = rtf_to_text(raw)
    elif ext in (".txt", ""):
        text = raw.decode("utf-8", "replace")
    else:
        raise ValueError(f"Формат {ext} не поддерживается: нужен PDF, HTML, RTF или TXT сводной редакции")
    if not text.strip():
        raise ValueError("Текст не извлечён: скан без текстового слоя (например, официальная публикация "
                         "pravo.gov.ru) — нужна текстовая сводная редакция")
    return text


# --- хранение --------------------------------------------------------------------------------------------------

CHUNKER = "law_norms"

SQL_UPSERT_ACT = """
INSERT INTO rm_legal_acts (act_key, kind, number, act_date, title, edition_date, edition_source, source_name,
                           text_sha256, document_id, norms_count)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (act_key, edition_date) DO UPDATE SET
    kind = EXCLUDED.kind, number = EXCLUDED.number, act_date = EXCLUDED.act_date, title = EXCLUDED.title,
    edition_source = EXCLUDED.edition_source, source_name = EXCLUDED.source_name, text_sha256 = EXCLUDED.text_sha256,
    document_id = EXCLUDED.document_id, norms_count = EXCLUDED.norms_count, loaded_at = NOW()
RETURNING id
"""

# редакция, действующая на дату: последняя с edition_date <= даты
SQL_EDITION_ON = """
SELECT id, act_key, kind, title, edition_date, document_id FROM rm_legal_acts
WHERE act_key = $1 AND edition_date <= $2 ORDER BY edition_date DESC LIMIT 1
"""


class LegalActsRepo:
    """Нормативные акты в Postgres сервиса (пул ``AIRepo.pool``)."""

    def __init__(self, pool: Any):
        """Запоминает пул ``asyncpg``."""
        self.pool = pool

    async def same_edition(self, act_key: str, edition_date: str, text_sha256: str) -> Optional[int]:
        """``id`` уже загруженной редакции с тем же текстом и посчитанными эмбеддингами.

        Редакция, сохранённая без эмбеддингов (сервис был недоступен), не считается загруженной: повторная загрузка
        того же файла досчитает их.
        """
        return await self.pool.fetchval(
            "SELECT a.id FROM rm_legal_acts a WHERE a.act_key = $1 AND a.edition_date = $2 AND a.text_sha256 = $3 "
            "AND EXISTS (SELECT 1 FROM ai_document_chunks c WHERE c.document_id = a.document_id AND c.chunker = $4 "
            "AND c.embedding IS NOT NULL)",
            act_key, date.fromisoformat(edition_date), text_sha256, CHUNKER)

    async def save_act(self, act: Dict[str, Any], document_id: Optional[int], source_name: Optional[str]) -> int:
        """Сохраняет редакцию акта и её нормы (нормы редакции перезаписываются).

        Args:
            act: Результат :func:`parse_act`.
            document_id: ``ai_documents.id`` текста.
            source_name: Имя загруженного файла.

        Returns:
            int: ``rm_legal_acts.id``.
        """
        norms = act["norms"]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                aid = await conn.fetchval(
                    SQL_UPSERT_ACT, act["act_key"], act["kind"], act.get("number"),
                    date.fromisoformat(act["act_date"]) if act.get("act_date") else None, act.get("title"),
                    date.fromisoformat(act["edition_date"]), act.get("edition_source"), source_name,
                    act.get("text_sha256"), document_id, len(norms))
                await conn.execute("DELETE FROM rm_legal_norms WHERE act_id = $1", aid)
                await conn.executemany(
                    "INSERT INTO rm_legal_norms (act_id, ord, segment, lvl1, lvl2, lvl3, lvl4, heading, address, text, "
                    "lost_force) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    [(aid, i, n.get("segment"), n.get("lvl1"), n.get("lvl2"), n.get("lvl3"), n.get("lvl4"),
                      n.get("heading"), n["address"], n["text"], n["lost_force"]) for i, n in enumerate(norms)])
        return int(aid)

    async def save_chunks(self, document_id: int, chunks: List[Dict[str, Any]],
                          embeddings: List[Optional[List[float]]], model: str) -> None:
        """Заменяет чанки норм документа (``chunker = law_norms``, ``heading`` — адрес части)."""
        from configs.ai_repo import _vec

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM ai_document_chunks WHERE document_id = $1 AND chunker = $2",
                                   document_id, CHUNKER)
                await conn.executemany(
                    "INSERT INTO ai_document_chunks (document_id, chunk_no, text, embedding, embedding_model, chunker, heading) "
                    "VALUES ($1, $2, $3, $4::vector, $5, $6, $7)",
                    [(document_id, i, c["text"], _vec(e) if e is not None else None, model, CHUNKER, c["address"])
                     for i, (c, e) in enumerate(zip(chunks, embeddings))])

    async def editions(self) -> List[Dict[str, Any]]:
        """Загруженные редакции (для просмотра)."""
        rows = await self.pool.fetch(
            "SELECT id, act_key, kind, number, act_date, title, edition_date, edition_source, source_name, norms_count, "
            "loaded_at FROM rm_legal_acts ORDER BY act_key, edition_date")
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()} for r in rows]

    async def edition_on(self, act_key: str, on: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Редакция акта, действующая на дату (по умолчанию сегодня)."""
        row = await self.pool.fetchrow(SQL_EDITION_ON, act_key, date.fromisoformat(on) if on else date.today())
        return dict(row) if row else None

    async def norms(self, act_id: int) -> List[Dict[str, Any]]:
        """Нормы редакции (для индекса проверки ссылок)."""
        rows = await self.pool.fetch(
            "SELECT segment, lvl1, lvl2, lvl3, lvl4, heading, address, text, lost_force FROM rm_legal_norms "
            "WHERE act_id = $1 ORDER BY ord", act_id)
        return [dict(r) for r in rows]


class LegalActsLoader:
    """Загрузка редакции акта: разбор, текст в ``ai_documents``, нормы, чанки с эмбеддингами.

    Args:
        repo: ``AIRepo``.
        http_manager: ``HTTPClientManager`` для клиента эмбеддингов (``None`` — без эмбеддингов).
    """

    def __init__(self, repo: Any, http_manager: Any = None):
        """Создаёт репозиторий актов."""
        self.repo = repo
        self.acts = LegalActsRepo(repo.pool)
        self.http_manager = http_manager

    async def load_text(self, text: str, source_name: Optional[str] = None,
                        edition_date: Optional[str] = None) -> Dict[str, Any]:
        """Загружает редакцию акта из текста.

        Args:
            text: Текст сводной редакции.
            source_name: Имя файла (для журнала и даты редакции).
            edition_date: Дата редакции ``YYYY-MM-DD``.

        Returns:
            Dict[str, Any]: ``act_id``, ``act_key``, ``edition_date``, ``edition_source``, ``norms``, ``chunks``,
            ``status`` (``loaded`` / ``unchanged``), ``warning`` — если дата редакции не указана.
        """
        act = parse_act(text, source_name, edition_date)
        out = {k: act.get(k) for k in ("act_key", "kind", "title", "edition_date", "edition_source")}
        out["norms"] = len(act["norms"])
        if act["edition_source"] in ("amendments", "act_date"):
            out["warning"] = ("Дата редакции не указана — взята " +
                              ("дата последнего изменяющего документа" if act["edition_source"] == "amendments"
                               else "дата принятия акта") + "; передайте edition_date")
        if act.get("warnings"):
            out["warnings"] = act["warnings"]
        same = await self.acts.same_edition(act["act_key"], act["edition_date"], act["text_sha256"])
        if same:
            return {**out, "act_id": same, "status": "unchanged", "chunks": 0}
        sha = act["text_sha256"]
        doc_id = await self.repo.upsert_document(sha, sha, ".txt", len(text.encode("utf-8")), text, reader="legal_acts")
        aid = await self.acts.save_act(act, int(doc_id), source_name)
        chunks = chunks_for_embedding(act["norms"])
        vectors: List[Optional[List[float]]] = [None] * len(chunks)
        model = "none"
        if self.http_manager is not None and chunks:
            try:
                from configs.config import Config
                from configs.embedding_client import EmbeddingClient

                e = Config().get_embedding_config()
                model = e.model
                emb = EmbeddingClient(e.api_url, e.api_key, e.model, batch_size=e.batch_size,
                                      http_manager=self.http_manager)
                vectors = list(await emb.get_embeddings([c["text"] for c in chunks], strict=True))
            except Exception as ex:  # noqa: BLE001 — без эмбеддингов нормы всё равно сохранены (проверка ссылок работает)
                logger.warning(f"Эмбеддинги {act['act_key']} ред. {act['edition_date']} не получены: "
                               f"{type(ex).__name__}: {ex}")
        await self.acts.save_chunks(int(doc_id), chunks, vectors, model)
        logger.info(f"Нормативный акт {act['act_key']} ред. {act['edition_date']} ({source_name}): норм "
                    f"{len(act['norms'])}, чанков {len(chunks)}")
        return {**out, "act_id": aid, "status": "loaded", "chunks": len(chunks),
                "embeddings": sum(1 for v in vectors if v is not None)}

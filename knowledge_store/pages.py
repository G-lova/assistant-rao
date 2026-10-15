"""Разбиение текста документа на страницы и чанки с сохранением номеров страниц."""
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Маркеры страниц, которые ставит FileReader: «Страница N (OCR): …» (develop) и «Page N:» (master).
PAGE_MARKER_RE = re.compile(r"^(?:Страница|Page)\s+(\d+)(?:\s*\(OCR\))?\s*:", re.MULTILINE)

PSEUDO_PAGE_CHARS = 3000  # размер «псевдостраницы» для документов без разметки страниц (DOCX/XLSX/TXT)


@dataclass
class Page:
    """Одна страница документа.

    Attributes:
        number: Номер страницы (с 1). У псевдостраниц — порядковый номер фрагмента.
        text: Текст страницы без маркера.
        pseudo: ``True``, если страница выделена по размеру, а не по маркеру из исходного файла.
    """
    number: int
    text: str
    pseudo: bool = False


@dataclass
class Chunk:
    """Фрагмент текста для эмбеддинга.

    Attributes:
        idx: Порядковый номер чанка в документе (с 0).
        page_from: Первая страница, которой касается чанк.
        page_to: Последняя страница, которой касается чанк.
        text: Текст чанка (дословный фрагмент документа — по нему проверяются цитаты).
        heading: Заголовок раздела, к которому относится начало чанка (только у нарезки ``pages_v2``).
    """
    idx: int
    page_from: int
    page_to: int
    text: str
    heading: Optional[str] = None


def _split_pseudo(text: str, size: int = PSEUDO_PAGE_CHARS) -> List[Page]:
    """Режет текст без маркеров на псевдостраницы по границам абзацев.

    Args:
        text: Исходный текст.
        size: Целевой размер псевдостраницы в символах (абзац длиннее ``size`` режется жёстко).

    Returns:
        List[Page]: Псевдостраницы, пронумерованные с 1.
    """
    pages: List[Page] = []
    buf = ""
    for para in text.split("\n"):
        while len(para) > size:  # слишком длинная строка — режем жёстко
            if buf:
                pages.append(Page(len(pages) + 1, buf.strip(), True))
                buf = ""
            pages.append(Page(len(pages) + 1, para[:size], True))
            para = para[size:]
        if len(buf) + len(para) + 1 > size and buf:
            pages.append(Page(len(pages) + 1, buf.strip(), True))
            buf = ""
        buf += para + "\n"
    if buf.strip():
        pages.append(Page(len(pages) + 1, buf.strip(), True))
    return pages


def split_pages(text: str) -> List[Page]:
    """Разбивает извлечённый текст на страницы.

    Если в тексте есть маркеры ``Страница N (OCR):`` / ``Page N:`` — используются они (текст до
    первого маркера, если он непустой, считается страницей 1 только при отсутствии маркера «1»).
    Иначе текст режется на псевдостраницы.

    Args:
        text: Текст, полученный из ``FileReader.read_file``.

    Returns:
        List[Page]: Список страниц (пустой для пустого текста).
    """
    if not text or not text.strip():
        return []
    matches = list(PAGE_MARKER_RE.finditer(text))
    if not matches:
        return _split_pseudo(text)
    pages: List[Page] = []
    head = text[:matches[0].start()].strip()
    if head:
        pages.append(Page(0, head))  # преамбула до первой страницы
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            pages.append(Page(int(m.group(1)), body))
    return pages


def chunk_pages(pages: List[Page], max_chars: int = 1500, overlap: int = 150) -> List[Chunk]:
    """Режет страницы на чанки заданного размера с перекрытием, сохраняя диапазон страниц.

    Чанк не пересекает границу страницы шире, чем нужно: текст страниц склеивается в поток,
    для каждого символа известна его страница, а чанку присваивается диапазон страниц его символов.

    Args:
        pages: Страницы документа.
        max_chars: Максимальный размер чанка в символах.
        overlap: Перекрытие соседних чанков в символах (должно быть меньше ``max_chars``).

    Returns:
        List[Chunk]: Чанки с ``page_from``/``page_to``.
    """
    if overlap >= max_chars:
        raise ValueError("overlap должен быть меньше max_chars")
    stream = ""
    owner: List[int] = []  # номер страницы для каждого символа потока
    for p in pages:
        piece = p.text + "\n"
        stream += piece
        owner.extend([p.number] * len(piece))
    chunks: List[Chunk] = []
    start = 0
    while start < len(stream):
        end = min(start + max_chars, len(stream))
        if end < len(stream):  # стараемся закончить на границе строки/предложения
            cut = max(stream.rfind("\n", start, end), stream.rfind(". ", start, end))
            if cut > start + max_chars // 2:
                end = cut + 1
        text = stream[start:end].strip()
        if text:
            chunks.append(Chunk(len(chunks), owner[start], owner[end - 1], text))
        if end >= len(stream):
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---------------------------------------------------------------- нарезка pages_v2 (по структуре документа)

CHUNKER_PAGES_V2 = "pages_v2"
"""Имя схемы нарезки в ``ai_document_chunks.chunker``: границы по пунктам и заголовкам, таблицы по строкам,
заголовок раздела отдельно, страницы сохраняются. Единая для экспертизы; РМ переводится после замера."""

HEADING_RE = re.compile(
    r"^\s*(?:(?:Раздел|РАЗДЕЛ|Глава|ГЛАВА|Статья|Часть|Приложение|ПРИЛОЖЕНИЕ)\b"
    r"|(?:\d{1,2}\.){1,3}\s*[А-ЯЁA-Z]"
    r"|\d{1,2}\.\s+[А-ЯЁA-Z]"
    r"|[IVX]{1,5}\.\s+[А-ЯЁA-Z])")
CLAUSE_RE = re.compile(r"^\s*(?:\d{1,2}\.){1,4}\d{0,2}\.?\s+\S")


@dataclass
class _Unit:
    """Неделимая единица текста при нарезке: строка, абзац или строка таблицы."""

    text: str
    page: int
    heading: bool = False
    table: bool = False
    table_header: Optional[str] = None


def _is_table_row(line: str) -> bool:
    """Строка таблицы: два и больше разделителя ``|`` или табуляции."""
    return line.count("|") >= 2 or line.count("\t") >= 2


def _is_heading(line: str) -> bool:
    """Заголовок: короткая строка с номером раздела/пункта или словом «Раздел», либо строка ПРОПИСНЫМИ."""
    t = line.strip()
    if not t or len(t) > 150 or t.endswith((",", ";")):
        return False
    if HEADING_RE.match(t):
        return True
    letters = [ch for ch in t if ch.isalpha()]
    return len(letters) >= 6 and sum(ch.isupper() for ch in letters) / len(letters) > 0.85


def _units(pages: List[Page]) -> List[_Unit]:
    """Разбивает страницы на единицы: строки (абзацы), заголовки и строки таблиц с шапкой."""
    units: List[_Unit] = []
    for page in pages:
        header: Optional[str] = None
        prev_table = False
        for raw in page.text.split("\n"):
            line = raw.rstrip()
            if not line.strip():
                prev_table = False
                continue
            if _is_table_row(line):
                if not prev_table:
                    header = line
                units.append(_Unit(line, page.number, table=True, table_header=header))
                prev_table = True
                continue
            prev_table = False
            units.append(_Unit(line, page.number, heading=_is_heading(line)))
    return units


def _split_long(unit: _Unit, max_chars: int) -> List[_Unit]:
    """Делит слишком длинную единицу по предложениям, а предложения — по пробелам."""
    if len(unit.text) <= max_chars:
        return [unit]
    parts: List[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?;])\s+", unit.text):
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if buf and len(buf) + 1 + len(sentence) > max_chars:
            parts.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf)
    return [_Unit(p, unit.page, False, unit.table, unit.table_header) for p in parts if p]


def _tail(text: str, limit: int) -> str:
    """Хвост строки не длиннее ``limit``: с начала предложения, а если его нет — с начала слова."""
    if len(text) <= limit:
        return text
    window = text[-limit:]
    m = re.search(r"(?<=[.!?;])\s+", window)
    if m and m.end() < len(window):
        return window[m.end():].strip()
    space = window.find(" ")
    return window[space + 1:].strip() if space >= 0 else ""


def chunk_structured(pages: List[Page], max_chars: int = 1800, overlap_chars: int = 200,
                     min_chars: int = 600) -> List[Chunk]:
    """Нарезка ``pages_v2``: чанки по структуре документа с сохранением страниц и заголовка раздела.

    * границы — между строками/абзацами, а не посреди слова; новый раздел или пункт (заголовок, «5.1.»)
      начинает новый чанк, если текущий уже не меньше ``min_chars``;
    * строки таблицы не рвутся; продолжение таблицы в следующем чанке начинается с её шапки;
    * заголовок раздела хранится в ``Chunk.heading`` (в эмбеддинг он добавляется отдельно —
      :func:`embedding_text`), текст чанка остаётся дословным фрагментом документа;
    * перекрытие — хвостовые строки предыдущего чанка общей длиной до ``overlap_chars``
      (если чанк начат с нового раздела или пункта, перекрытие не нужно).

    Размер задан в символах: около 1800 символов русского текста — 400–600 токенов модели эмбеддингов.

    Args:
        pages: Страницы документа (:func:`split_pages`).
        max_chars: Максимальный размер чанка в символах.
        overlap_chars: Максимальное перекрытие соседних чанков.
        min_chars: Минимальный размер чанка, после которого заголовок начинает новый чанк.

    Returns:
        List[Chunk]: Чанки с ``page_from``/``page_to`` и ``heading``.
    """
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars должен быть меньше max_chars")
    units: List[_Unit] = []
    for u in _units(pages):
        units.extend(_split_long(u, max_chars))

    chunks: List[Chunk] = []
    current: List[_Unit] = []
    size = 0
    heading: Optional[str] = None          # заголовок, действующий на текущей позиции
    chunk_heading: Optional[str] = None    # заголовок, действовавший в начале текущего чанка

    def flush() -> List[_Unit]:
        """Закрывает текущий чанк и возвращает его единицы."""
        body = [u.text for u in current]
        chunks.append(Chunk(len(chunks), current[0].page, current[-1].page, "\n".join(body), chunk_heading))
        return list(current)

    for unit in units:
        boundary = unit.heading or CLAUSE_RE.match(unit.text) is not None
        add = len(unit.text) + (1 if current else 0)
        if current and (size + add > max_chars or (boundary and size >= min_chars)):
            done = flush()
            carry: List[_Unit] = []
            if not boundary:
                tail = 0
                for prev in reversed(done):          # перекрытие: хвостовые строки предыдущего чанка
                    if prev.heading or tail + len(prev.text) + 1 > overlap_chars:
                        break
                    carry.insert(0, prev)
                    tail += len(prev.text) + 1
                if not carry and not done[-1].heading and not done[-1].table:
                    piece = _tail(done[-1].text, overlap_chars)   # длинная строка: хвост по границе предложения/слова
                    if piece:
                        carry.append(_Unit(piece, done[-1].page))
            if unit.table and unit.table_header and unit.table_header != unit.text \
                    and not any(c.text == unit.table_header for c in carry):
                carry.insert(0, _Unit(unit.table_header, unit.page, table=True, table_header=unit.table_header))
            while carry and sum(len(c.text) + 1 for c in carry) + len(unit.text) > max_chars:
                # не превышать лимит: убираем самую раннюю строку перекрытия, шапку таблицы оставляем до последнего
                carry.pop(1 if len(carry) > 1 and carry[0].text == unit.table_header else 0)
            current = carry
            size = sum(len(c.text) + 1 for c in carry) - (1 if carry else 0)
            chunk_heading = heading
            add = len(unit.text) + (1 if current else 0)
        if not current:
            chunk_heading = unit.text.strip() if unit.heading else heading
        if unit.heading:
            heading = unit.text.strip()
        current.append(unit)
        size += add
    if current:
        flush()
    return chunks


def embedding_text(chunk: Chunk) -> str:
    """Текст для эмбеддинга: заголовок раздела + текст чанка (если чанк не начинается с этого заголовка).

    Args:
        chunk: Чанк.

    Returns:
        str: Текст, который отправляется в модель эмбеддингов.
    """
    if chunk.heading and not chunk.text.lstrip().startswith(chunk.heading):
        return f"{chunk.heading}\n{chunk.text}"
    return chunk.text


# ---------------------------------------------------------------- выбор нарезки экспертизы

CHUNKER_PAGES_1500 = "pages_1500"
"""Прежняя нарезка экспертизы (``pe_chunks`` до миграции 005): 1500 символов, перекрытие 150, страницы сохраняются."""

CHUNKERS = {
    CHUNKER_PAGES_1500: lambda pages: chunk_pages(pages, max_chars=1500, overlap=150),
    CHUNKER_PAGES_V2: lambda pages: chunk_structured(pages, max_chars=1800, overlap_chars=200),
}


def current_chunker() -> str:
    """Нарезка экспертизы для индексации и поиска фактов (``PE_CHUNKER``).

    По умолчанию — прежняя ``pages_1500``: замер 14.10.2026 (1511 фактов) показал, что ``pages_v2`` находит
    фрагмент с цитатой реже (recall@8 0,79 против 0,96). Переключение — после диагностики
    ``scripts.eval_chunkers variants`` и переиндексации (``scripts.reindex_pages_v2``).

    Returns:
        str: Имя нарезки из :data:`CHUNKERS`.

    Raises:
        ValueError: Неизвестная нарезка.
    """
    import os
    name = os.getenv("PE_CHUNKER", CHUNKER_PAGES_1500).strip()
    if name not in CHUNKERS:
        raise ValueError(f"Неизвестная нарезка PE_CHUNKER={name!r}; допустимо: {', '.join(CHUNKERS)}")
    return name


def chunk_document(text: str, chunker: str) -> List[Chunk]:
    """Режет текст документа выбранной нарезкой.

    Args:
        text: Текст документа.
        chunker: Имя нарезки из :data:`CHUNKERS`.

    Returns:
        List[Chunk]: Чанки.
    """
    return CHUNKERS[chunker](split_pages(text))

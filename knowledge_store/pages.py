"""Разбиение текста документа на страницы и чанки с сохранением номеров страниц."""
import re
from dataclasses import dataclass
from typing import List

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
        text: Текст чанка.
    """
    idx: int
    page_from: int
    page_to: int
    text: str


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

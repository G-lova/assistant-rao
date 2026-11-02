"""Текст .xlsx напрямую из ZIP (запасной путь, когда pandas/openpyxl не видят листов).

Бывают книги, у которых в ``xl/workbook.xml`` нет обычных листов (только диаграммы, «строгий» OOXML, книга после
сторонних конвертеров): ``pd.read_excel`` падает с «Sheet name is an empty list» (файл РМ 21.10.2026), и файл при
каждом запуске уходит в ошибку. Здесь читаются сами XML листов: общие строки (``sharedStrings``), строки в ячейках
и значения — в порядке ячеек, лист за листом.
"""
import re
import zipfile
from typing import List

CELL_RE = re.compile(r"<c\b([^>]*)>(.*?)</c>", re.S)
VALUE_RE = re.compile(r"<v>(.*?)</v>", re.S)
TEXT_RE = re.compile(r"<t\b[^>]*>(.*?)</t>", re.S)
ROW_RE = re.compile(r"<row\b[^>]*>(.*?)</row>", re.S)
MAX_CHARS = 2_000_000
MAX_ROWS = 2000   # непустых строк на лист: лист сметы бывает на сотни тысяч строк


def _unescape(s: str) -> str:
    """XML-сущности → символы."""
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            .replace("&amp;", "&"))


def xlsx_raw_text(path: str) -> str:
    """Текст всех листов книги без разбора структуры книги.

    Args:
        path: Путь к .xlsx.

    Returns:
        str: «Лист N: …» с ячейками через « | », строки — с новой строки; пусто, если текста нет или это не ZIP.
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return ""
    with zf:
        names = zf.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"<si\b[^>]*>(.*?)</si>", xml, re.S):
                shared.append(_unescape("".join(TEXT_RE.findall(si))))
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/[^/]+\.xml", n))
        out, size = [], 0
        for n, name in enumerate(sheets, 1):
            xml = zf.read(name).decode("utf-8", "ignore")
            lines = []
            for row in ROW_RE.findall(xml):
                cells = []
                for attrs, body in CELL_RE.findall(row):
                    if 't="s"' in attrs:
                        v = VALUE_RE.search(body)
                        try:
                            cells.append(shared[int(v.group(1))] if v else "")
                        except (ValueError, IndexError):
                            continue
                    elif 't="inlineStr"' in attrs:
                        cells.append(_unescape("".join(TEXT_RE.findall(body))))
                    else:
                        v = VALUE_RE.search(body)
                        if v:
                            cells.append(_unescape(v.group(1)))
                cells = [c.strip() for c in cells if c and c.strip()]
                if cells:
                    lines.append(" | ".join(cells))
                    if len(lines) >= MAX_ROWS:
                        break
            if lines:
                block = f"Лист {n}:\n" + "\n".join(lines)
                out.append(block)
                size += len(block)
                if size > MAX_CHARS:
                    break
        return "\n\n".join(out)[:MAX_CHARS]

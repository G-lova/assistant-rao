"""Проверка критерия мониторинга «обоснование НМЦК» по папкам закупок (прототип).

Для каждой папки закупки (контракт/договор) читает PDF/DOCX/TXT, при отсутствии текстового слоя
распознаёт сканы (``pdftoppm`` + ``tesseract -l rus``), применяет правила
``knowledge_store.monitoring_nmck`` и печатает вердикт с цитатами. Распознанный текст кэшируется
рядом с результатом, чтобы повторные прогоны не тратили время.

Пример::

    python3 scripts/check_monitoring_nmck.py --out /tmp/nmck.json "/путь/Контракт 106" "/путь/Договор 1121"
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_store import monitoring_nmck as nm  # noqa: E402

OCR_DPI = 200          # разрешение растеризации страниц для распознавания
OCR_MAX_PAGES = 6      # обоснование цены умещается в первые страницы; длинные файлы не распознаём целиком
MIN_TEXT_CHARS = 500   # меньше — считаем, что текстового слоя нет (скан)


def pdf_text(path: Path, cache_dir: Path) -> str:
    """Текст PDF: сначала текстовый слой, иначе распознавание первых страниц (с кэшем по пути и размеру)."""
    key = hashlib.md5(f"{path}:{path.stat().st_size}".encode()).hexdigest()
    cached = cache_dir / f"{key}.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True).stdout
    if len(out.strip()) < MIN_TEXT_CHARS:
        parts = []
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdftoppm", "-r", str(OCR_DPI), "-l", str(OCR_MAX_PAGES), "-png", str(path),
                            f"{tmp}/p"], check=False)
            for png in sorted(Path(tmp).glob("p*.png")):
                parts.append(subprocess.run(["tesseract", str(png), "-", "-l", "rus"], capture_output=True,
                                            text=True).stdout)
        out = "\n".join(parts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(out, encoding="utf-8")
    return out


def docx_text(path: Path) -> str:
    """Текст DOCX (абзацы и ячейки таблиц) через python-docx."""
    import docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def read_purchase(folder: Path, cache_dir: Path) -> list:
    """Читает документы папки закупки (рекурсивно); файлы с «служебными» именами не распознаются."""
    docs = []
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        if f.suffix.lower() not in (".pdf", ".docx", ".txt"):
            continue
        if nm.should_skip_by_name(f.stem):
            docs.append(nm.Doc(f.name, None))
            continue
        if f.suffix.lower() == ".pdf":
            text = pdf_text(f, cache_dir)
        elif f.suffix.lower() == ".docx":
            text = docx_text(f)
        else:
            text = f.read_text(encoding="utf-8", errors="replace")
        docs.append(nm.Doc(f.name, text))
    return docs


def main() -> None:
    """Точка входа: принимает папки закупок, печатает и сохраняет вердикты."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="+", help="Папки закупок (контракт/договор)")
    ap.add_argument("--out", help="JSON с результатом")
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "nmck_ocr_cache"))
    args = ap.parse_args()
    res = {}
    for folder in args.folders:
        r = nm.check_purchase(read_purchase(Path(folder), Path(args.cache)))
        res[folder] = r
        print(f"{r['verdict']:20s} {folder}")
        for f in r["findings"]:
            print(f"   [{f['rule']}] {f['message']}  ({', '.join(f['files'])})")
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

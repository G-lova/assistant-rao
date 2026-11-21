
import aiofiles
import asyncio
import bz2
import gzip
import json
import lzma
import os
import pandas as pd
import py7zr
import rarfile
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from docx import Document
from pathlib import Path
from typing import Dict, Optional
from pptx import Presentation
from pdf2image import convert_from_path

from configs.logger import get_logger
from configs.rate_limiter import llm_rate_limiter
from evaluate_documents.ocr import OCRProcessor
from configs.file_errors import BrokenFile  # повреждённый файл (лёгкий модуль: его ловит анализ файлов РМ)


logger = get_logger(__name__)


def excel_limits() -> dict:
    """Ограничения чтения Excel из настроек (``EXCEL_MAX_ROWS``, ``EXCEL_MAX_COLS``, ``EXCEL_MAX_SHEETS``,
    ``EXCEL_MAX_CHARS``); без настроек — значения по умолчанию."""
    try:
        from configs import settings as cfg
    except Exception:  # noqa: BLE001 — модуль используется и без настроек
        cfg = None
    return {"max_rows": int(getattr(cfg, "EXCEL_MAX_ROWS", 500)), "max_cols": int(getattr(cfg, "EXCEL_MAX_COLS", 60)),
            "max_sheets": int(getattr(cfg, "EXCEL_MAX_SHEETS", 20)),
            "max_chars": int(getattr(cfg, "EXCEL_MAX_CHARS", 300_000))}


def xlsx_text_limited(path: str, max_rows: int = 500, max_cols: int = 60, max_sheets: int = 20,
                      max_chars: int = 300_000) -> str:
    """Текст .xlsx потоковым чтением (openpyxl ``read_only``) с ограничениями — память и время не зависят от
    размеченного размера листа.

    Пустые строки и ячейки пропускаются; читается не больше ``max_rows`` непустых строк и ``max_cols`` столбцов на
    лист, ``max_sheets`` листов и ``max_chars`` символов всего. Обрезанный лист помечается.

    Args:
        path: Путь к .xlsx.
        max_rows: Непустых строк на лист.
        max_cols: Столбцов на строку.
        max_sheets: Листов.
        max_chars: Символов всего.

    Returns:
        str: «Лист: имя», затем строки с ячейками через « | »; листы через пустую строку.
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out, size = [], 0
    try:
        for n, ws in enumerate(wb.worksheets):
            if n >= max_sheets:
                out.append(f"[Листов больше {max_sheets}, остальные не прочитаны]")
                break
            lines, scanned = [], 0
            for row in ws.iter_rows(max_col=max_cols, values_only=True):
                scanned += 1
                cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if cells:
                    lines.append(" | ".join(cells))
                    if len(lines) >= max_rows:
                        lines.append(f"[Лист обрезан: прочитано {max_rows} строк]")
                        break
                if scanned >= max_rows * 20:   # лист из пустых строк (форматирование до конца листа)
                    break
            if lines:
                block = f"Лист: {ws.title}\n" + "\n".join(lines)
                out.append(block)
                size += len(block)
                if size >= max_chars:
                    break
    finally:
        wb.close()
    return "\n\n".join(out)[:max_chars].strip()


SOFFICE_TIMEOUT = 60


async def run_soffice(args: list, timeout: int = SOFFICE_TIMEOUT) -> subprocess.CompletedProcess:
    """Запускает LibreOffice (``soffice --headless``) со своим временным профилем.

    - Профиль (``lo_profile_*``) удаляется после запуска — раньше каталог оставался в /tmp после каждой
      конвертации.
    - Процесс запускается в своей группе; по таймауту завершается вся группа. ``subprocess.run(timeout=…)``
      завершал только запускающий скрипт, а ``soffice.bin`` продолжал работать и занимать процессор
      (06.11.2026: «Контракт 464-ЭК_2026.docx», конвертация дольше 60 с).
    - Свой профиль у каждого запуска: параллельные конвертации не мешают друг другу.

    Args:
        args: Аргументы после ``soffice --headless …`` (``--convert-to``, ``--outdir``, файл).
        timeout: Таймаут, секунд.

    Returns:
        subprocess.CompletedProcess: Результат с ``stdout`` и ``stderr``.

    Raises:
        subprocess.TimeoutExpired: Не уложился в таймаут (процессы завершены).
        subprocess.CalledProcessError: Ненулевой код возврата.
        FileNotFoundError: LibreOffice не установлен.
    """
    import signal

    profile = tempfile.mkdtemp(prefix="lo_profile_")
    cmd = ["soffice", "--headless", "--norestore", "--nofirststartwizard",
           f"-env:UserInstallation=file://{profile}", *args]

    def run() -> subprocess.CompletedProcess:
        """Запуск с ожиданием; по таймауту — завершение группы процессов."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    try:
        return await asyncio.to_thread(run)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


PDF_OCR_DPI = 140          # разрешение страниц для OCR в экспертизе (как было)


def pdf_text_layer_enabled() -> bool:
    """Брать ли текст PDF из текстового слоя (настройка ``EVAL_PDF_TEXT_LAYER``, по умолчанию да)."""
    try:
        from configs import settings as cfg
    except Exception:  # noqa: BLE001 — модуль используется и без настроек
        return True
    return bool(getattr(cfg, "EVAL_PDF_TEXT_LAYER", True))


def _ocr_image_pages() -> bool:
    """Отправлять ли в OCR страницы с растровыми изображениями (``EVAL_PDF_OCR_IMAGE_PAGES``, по умолчанию да)."""
    try:
        from configs import settings as cfg
    except Exception:  # noqa: BLE001
        return True
    return bool(getattr(cfg, "EVAL_PDF_OCR_IMAGE_PAGES", True))


def _xobjects_have_image(resources, depth: int = 0) -> bool:
    """Есть ли среди XObject ресурсов страницы растровое изображение (с заходом в формы, до 3 уровней).

    Args:
        resources: Словарь ``/Resources`` страницы или формы (pypdf).
        depth: Текущая глубина вложенности форм.

    Returns:
        bool: ``True`` — есть ``/Subtype /Image``.
    """
    if resources is None or depth > 3:
        return False
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
    if xobjects is None:
        return False
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    for ref in xobjects.values():
        obj = ref.get_object() if hasattr(ref, "get_object") else ref
        subtype = obj.get("/Subtype")
        if subtype == "/Image":
            return True
        if subtype == "/Form" and _xobjects_have_image(obj.get("/Resources"), depth + 1):
            return True
    return False


def pdf_image_pages(path: str) -> list:
    """Флаги «на странице есть растровое изображение» по страницам PDF.

    Сначала pypdf (смотрит только словари ресурсов, изображения не декодирует — быстро), иначе pdfplumber.

    Args:
        path: Путь к PDF.

    Returns:
        list: ``[bool, …]`` по страницам; пустой список — определить не удалось.
    """
    try:
        from pypdf import PdfReader

        return [_xobjects_have_image(page.get("/Resources")) for page in PdfReader(path).pages]
    except Exception as e:  # noqa: BLE001 — пробуем pdfplumber
        logger.debug(f"PDF {path}: pypdf не определил изображения: {type(e).__name__}: {e}")
    try:
        import pdfplumber
    except ImportError:   # pdfplumber не входит в requirements — запасной путь только при ручной установке
        return []
    try:
        with pdfplumber.open(path) as pdf:
            return [bool(page.images) for page in pdf.pages]
    except Exception as e:  # noqa: BLE001
        logger.info(f"PDF {path}: изображения на страницах не определены: {type(e).__name__}: {e}")
        return []


def pdf_layer_pages(path: str) -> list:
    """Текстовый слой PDF по страницам и пригодность каждой страницы без OCR.

    Страница пригодна, если текст слоя проходит ``risk_monitoring.pdf_text.layer_page_ok`` и (при
    ``EVAL_PDF_OCR_IMAGE_PAGES``) на ней нет растровых изображений. Если изображения определить не удалось,
    страница считается непригодной — она уйдёт в OCR, как раньше.

    Args:
        path: Путь к PDF.

    Returns:
        list: ``[(текст, пригодна), …]``; пустой список — слой не прочитан (все страницы — в OCR).
    """
    from risk_monitoring.pdf_text import layer_page_ok, text_layer

    texts = text_layer(path)
    if not texts:
        return []
    images = pdf_image_pages(path) if _ocr_image_pages() else [False] * len(texts)
    if len(images) != len(texts):
        images = [True] * len(texts)
    return [(t, layer_page_ok(t) and not img) for t, img in zip(texts, images)]


def pdf_page_count(path: str) -> int:
    """Число страниц PDF (``pdfinfo``); 0 — не определено."""
    try:
        from pdf2image import pdfinfo_from_path

        return int(pdfinfo_from_path(path).get("Pages") or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDF {path}: число страниц не определено: {type(e).__name__}: {e}")
        return 0


def _frames_text(excel_data: dict) -> str:
    """Текст листов, прочитанных pandas (.xls): «Лист: имя» и таблица (до 20 строк), пробелы схлопнуты."""
    result = []
    for sheet_name, df in excel_data.items():
        result.append(f"Лист: {sheet_name}")
        df = df.fillna('').astype(str)
        for col in df.columns:
            if 'unnamed' in str(col).lower():
                df = df.rename(columns={col: f'Колонка_{df.columns.get_loc(col)}'})
        result.append(df.to_string(index=False, max_rows=20))
        result.append("")
    return re.sub(r'\s+', ' ', "\n".join(result).strip())


# Служебные файлы в архивах: не документы (временные файлы Word/Excel «~$…», метаданные macOS и Windows)
_ARCHIVE_JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
ARCHIVE_RANK_DOC, ARCHIVE_RANK_TABLE, ARCHIVE_RANK_OTHER, ARCHIVE_RANK_IMAGE = 0, 1, 2, 3
_DOC_EXT = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".html", ".htm", ".xml"}
_TABLE_EXT = {".xlsx", ".xls", ".csv"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}


# Предел длины одной части пути в байтах UTF-8 (NAME_MAX 255 у ext4/overlayfs, с запасом)
_MEMBER_NAME_BYTES = 240


def zip_name(name: str) -> str:
    """Имя члена zip без флага UTF-8: zipfile декодирует его как cp437; архивы из Windows пишут cp866.

    Args:
        name: Имя от zipfile.

    Returns:
        str: Имя в cp866, если оно так перекодируется, иначе исходное.
    """
    try:
        return name.encode("cp437").decode("cp866")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _short_part(part: str) -> str:
    """Часть пути не длиннее ``_MEMBER_NAME_BYTES`` байт: начало имени + ``~хеш`` + расширение."""
    if len(part.encode("utf-8")) <= _MEMBER_NAME_BYTES:
        return part
    import hashlib

    stem, ext = os.path.splitext(part)
    if len(ext.encode("utf-8")) > 16:   # «расширение» из длинного хвоста после точки — не расширение
        stem, ext = part, ""
    tag = "~" + hashlib.sha1(part.encode("utf-8")).hexdigest()[:8]
    room = _MEMBER_NAME_BYTES - len(tag.encode("utf-8")) - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:room].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{stem}{tag}{ext}"


def safe_member_path(name: str, output_dir: str) -> str:
    """Безопасный относительный путь члена архива: без «..», абсолютных путей и слишком длинных частей.

    Совпадения после сокращения различаются суффиксом ``(2)``, ``(3)`` …

    Args:
        name: Имя члена в архиве.
        output_dir: Каталог распаковки (проверка, что файл с таким именем ещё не записан).

    Returns:
        str: Относительный путь (пусто — имя без пригодных частей).
    """
    parts = [p for p in re.split(r"[\\/]+", name or "") if p not in ("", ".", "..")]
    parts = [_short_part(p) for p in parts]
    if not parts:
        return ""
    rel = os.path.join(*parts)
    stem, ext = os.path.splitext(rel)
    n = 2
    while os.path.exists(os.path.join(output_dir, rel)):
        rel = f"{stem}({n}){ext}"
        n += 1
    return rel


def is_archive_junk(relative_name: str) -> bool:
    """Служебный файл архива: ``~$*.docx`` (файл-владелец открытого документа Word, 162 байта — LibreOffice и
    python-docx его не открывают), ``._*`` и ``__MACOSX`` (macOS), ``Thumbs.db``, ``.DS_Store``, ``desktop.ini``."""
    parts = re.split(r"[\\/]", relative_name or "")
    base = parts[-1] if parts else ""
    return (base.startswith("~$") or base.startswith("._") or base.lower() in _ARCHIVE_JUNK_NAMES
            or "__MACOSX" in parts)


# Расширения, которые читает read_file (остальные дают лишь строку «Бинарный файл …»)
_READABLE_EXT = {".txt", ".csv", ".json", ".pdf", ".xlsx", ".xls", ".pptx", ".doc", ".docx", ".odt", ".rtf",
                 ".html", ".xml", ".sig", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff",
                 ".zip", ".rar", ".7z", ".tar", ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz", ".bin"}


def is_readable_name(relative_name: str) -> bool:
    """Файл архива стоит читать: формат, который понимает ``read_file``, или без расширения (определяется по
    содержимому). Чертежи (``.dwg``, ``.dwl``), временные ``.tmp`` и пр. — нет (21.11.2026, архив «РД (КЖ …).rar»)."""
    ext = os.path.splitext(relative_name or "")[1].lower()
    return not ext or ext in _READABLE_EXT


def archive_entry_rank(relative_name: str) -> int:
    """Очередь чтения файла архива при пределах: документы, таблицы, прочее (вложенные архивы …), картинки."""
    ext = os.path.splitext(relative_name or "")[1].lower()
    if ext in _DOC_EXT:
        return ARCHIVE_RANK_DOC
    if ext in _TABLE_EXT:
        return ARCHIVE_RANK_TABLE
    if ext in _IMAGE_EXT:
        return ARCHIVE_RANK_IMAGE
    return ARCHIVE_RANK_OTHER


class FileReader:
    """
    """
    def __init__(self, llm_client, model):
        self.ocr_processor = OCRProcessor(llm_client, model)
        # Пределы чтения архива (process_archive): None — архив читается целиком (экспертиза); риск-мониторинг
        # задаёт словарь max_files / max_images / max_chars / concurrency (settings.RM_ARCHIVE_*)
        self.archive_limits: Optional[Dict[str, int]] = None
        # Предел страниц PDF на OCR (read_pdf): None — все страницы без текстового слоя (экспертиза);
        # риск-мониторинг задаёт settings.RM_OCR_MAX_PAGES
        self.max_ocr_pages: Optional[int] = None
        # из них — последние страницы (подписи, печати, реквизиты сторон в конце документа)
        self.ocr_tail_pages: int = 0

        self.semaphore = asyncio.Semaphore(3)
        self.rate_limiter = llm_rate_limiter()  # общий на процесс лимит частоты запросов к модели


    async def read_txt_file(self, file_path: str) -> str:
        """
        Читает и возвращает содержимое текстового файла в кодировке UTF-8.

        Args:
            file_path (str): Путь к .txt файлу, который необходимо прочитать.

        Returns:
            str: Содержимое файла в виде строки. Возвращает пустую строку, если файл пустой.
        """
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            return await f.read()


    async def convert_to_pdf(self, file_path: str, ext: str, original_filename: str) -> str:
        """
        Конвертирует DOC/DOCX в PDF и передает PDF на дальнейшее чтение.

        При ошибке конвертации возвращается к обычному способу чтения
        исходного документа.
        """
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)

                logger.info(f"Конвертация {original_filename} в PDF...")
                result = await run_soffice(
                    ["--convert-to", "pdf:writer_pdf_Export", "--outdir", str(tmpdir_path), file_path])
                
                # LibreOffice сохраняет PDF с тем же именем, что и исходный файл
                pdf_path = tmpdir_path / f"{Path(file_path).stem}.pdf"
                if not pdf_path.exists():
                    raise FileNotFoundError(f"LibreOffice не создал PDF. STDERR: {result.stderr}")

                logger.info(f"Файл {original_filename} успешно конвертирован в PDF: {pdf_path}")
                return await self.read_pdf_file(str(pdf_path), original_filename)

            
        except subprocess.TimeoutExpired:
            # большой документ: без трассировки, текст читается напрямую
            logger.warning(f"Конвертация {ext.upper()} → PDF для {original_filename} дольше {SOFFICE_TIMEOUT} с — "
                           f"читаем {ext.upper()} напрямую")
            return await self.read_doc_file(file_path, ext, original_filename)
        except Exception as e:
            logger.error(f"Ошибка конвертации {ext.upper()} → PDF для {original_filename}: {e}", exc_info=True)
            logger.info(f"Пробуем обычный способ чтения {ext.upper()} файла: {original_filename}")
            return await self.read_doc_file(file_path, ext, original_filename)


    async def read_doc_file(self, file_path: str, ext: str, original_filename:str) -> str:
        """
        Читает файл формата .doc или .docx и извлекает из него текстовое содержимое.

        Для .docx дополнительно извлекается текст с изображений с помощью OCR.
        Проверяет существование файла, его размер и поддержку формата.

        Args:
            file_path (str): Путь к файлу .doc или .docx.

        Raises:
            ValueError: Если файл не существует.
            ValueError: Если файл пустой.
            ValueError: Если формат файла не поддерживается (не .doc и не .docx).
            ValueError: Если возникает ошибка при чтении файла .doc.
            ValueError: Если происходит ошибка на этапе обработки.

        Returns:
            str: Извлечённый текст из документа. В случае ошибки — исключение.
        """
        try:
            if not os.path.exists(file_path):
                raise ValueError("Файл не существует")
            if os.path.getsize(file_path) == 0:
                raise ValueError("Файл пустой")
            if ext == '.docx':
                return await self.extract_text_and_images_from_docx(file_path, original_filename, ocr_func=self.ocr_processor.ocr_image_with_qwen_vl)
            elif ext in {'.doc', '.odt', '.rtf'}:
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmpdir = Path(tmpdir)

                    # Пробуем конвертацию DOC → DOCX через LibreOffice
                    try:
                        # таймаут и свой профиль: раньше без таймаута — зависший LibreOffice держал прогон
                        await run_soffice(["--convert-to", "docx", "--outdir", str(tmpdir), file_path])

                        docx_path = tmpdir / (Path(file_path).stem + ".docx")
                        if docx_path.exists():
                            return await self.extract_text_and_images_from_docx(
                                str(docx_path),
                                original_filename,
                                ocr_func=self.ocr_processor.ocr_image_with_qwen_vl
                            )

                    except Exception as e:
                        logger.warning(f"LibreOffice не сработал для {original_filename}: {e}. Пробуем antiword...")
                        
                        if ext == '.doc':
                            try:
                                # Пробуем antiword
                                # .stdout — у результата, а не у корутины (раньше antiword не срабатывал никогда)
                                res = await asyncio.to_thread(
                                    subprocess.run, ["antiword", file_path], capture_output=True, text=True,
                                    timeout=SOFFICE_TIMEOUT)
                                text = (res.stdout or "").strip()

                                if text:
                                    return text
                                else:
                                    raise ValueError("antiword не вернул текст")

                            except Exception as e:
                                logger.warning(f"antiword не сработал: {e}. Пробуем catdoc...")

                                # Используем catdoc
                                try:
                                    result = await asyncio.to_thread(
                                        subprocess.run,
                                        ["catdoc", file_path],
                                        capture_output=True,
                                        text=True
                                    )
                                    return result.stdout.strip()
                                except Exception as e2:
                                    raise ValueError(f"Ошибка чтения DOC: {e2}")
                
            else:
                raise ValueError("Неподдерживаемый формат файла")
            
        except Exception as e:
            logger.error(f"Ошибка обработки документа: {str(e)}")
            raise ValueError(f"Ошибка обработки файла: {str(e)}")


    async def read_pptx_file(self, file_path: str) -> str:
        """
        Извлекает текстовое содержимое из презентации PowerPoint (.pptx).

        Проходит по всем слайдам и извлекает текст из всех доступных фигур (shape),
        включая заголовки, пункты списка и другие текстовые блоки.

        Args:
            file_path (str): Путь к файлу .pptx.

        Returns:
            str: Объединённый текст всех слайдов, разделённый переносами строк.
                Возвращает пустую строку, если текст не найден.
        """
        presentation = await asyncio.to_thread(Presentation, file_path)
        text_content = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text_content.append(shape.text)
        return "\n".join(text_content)


    async def read_pdf_file(self, file_path: str, original_filename:str) -> str:
        """
        Извлекает текст из PDF: текстовый слой там, где он пригоден, OCR (Qwen-VL) — для остальных страниц.

        Раньше в OCR шла каждая страница, даже если в PDF уже был текст (в том числе все документы Word,
        которые читаются через конвертацию в PDF). Теперь страница берётся из текстового слоя, если:

        - включена настройка ``EVAL_PDF_TEXT_LAYER``;
        - текст слоя пригоден (``risk_monitoring.pdf_text.layer_page_ok``: достаточно букв, нет ``(cid:NN)``
          и «кракозябр»);
        - на странице нет растровых изображений (при ``EVAL_PDF_OCR_IMAGE_PAGES``). Изображение на странице —
          это может быть скан, подпись или печать: модель OCR описывает их в ``visual_notes``, а проверка
          полноты документов на это опирается, поэтому такие страницы по-прежнему распознаются моделью.

        Страницы для OCR рендерятся по одной сразу в JPEG (``paths_only``), так что в памяти не держатся
        изображения всех страниц документа.

        Разметка: ``Page N:`` + ``Страница N: <текст слоя>`` или ``Page N:`` + ``Страница N (OCR): <ответ OCR>``
        (как раньше) — оба варианта понимает ``knowledge_store.pages.PAGE_MARKER_RE``.

        Args:
            file_path (str): Путь к PDF-файлу.
            original_filename (str): Имя исходного файла (для журнала и OCR).

        Raises:
            ValueError: Если произошла ошибка при чтении или обработке файла.

        Returns:
            str: Объединённый текст всех страниц.
                Если текст не распознан, возвращает "[Нет читаемого текста]".
        """
        async def process_page(i: int, temp_dir: str) -> str:
            """Рендерит одну страницу сразу в JPEG и распознаёт её (в памяти — только эта страница)."""
            async with self.semaphore:
                paths = await asyncio.to_thread(
                    convert_from_path, file_path, dpi=PDF_OCR_DPI, first_page=i + 1, last_page=i + 1,
                    output_folder=temp_dir, fmt="jpeg", paths_only=True, output_file=f"page_{i + 1}",
                )
                if not paths:
                    raise ValueError("страница не отрисована")
                ocr_text = await self.ocr_processor.ocr_image_with_qwen_vl(paths[0], original_filename)
                try:
                    os.remove(paths[0])          # изображение больше не нужно — не копим их во временной папке
                except OSError:
                    pass
                return f"Страница {i+1} (OCR): {ocr_text}"

        try:
            layer = await asyncio.to_thread(pdf_layer_pages, file_path) if pdf_text_layer_enabled() else []
            total = len(layer) or await asyncio.to_thread(pdf_page_count, file_path)
            if not total:
                # ни pypdf, ни poppler не прочитали структуру (нет trailer / xref, «EOF marker not found»):
                # файл повреждён или обрезан — повторное чтение того же файла ничего не даст
                raise BrokenFile("не удалось определить число страниц — PDF повреждён или обрезан")
            need_ocr = [i for i in range(total) if i >= len(layer) or not layer[i][1]]
            limit = getattr(self, "max_ocr_pages", None)
            over_limit = set()
            if limit and len(need_ocr) > limit:
                # скан на сотни страниц в РМ: 21.11.2026 «ПСД (Раздел 9-10).zip» читался 837 с. Распознаются
                # начало (предмет, цена, сроки) и конец документа (подписи, печати, реквизиты сторон) — середина
                # помечается
                tail = min(max(0, int(getattr(self, "ocr_tail_pages", 0) or 0)), limit)
                head = limit - tail
                keep = need_ocr[:head] + (need_ocr[-tail:] if tail else [])
                over_limit = set(need_ocr) - set(keep)
                need_ocr = keep
                logger.info(f"PDF {original_filename}: на OCR {len(need_ocr) + len(over_limit)} страниц — "
                            f"распознаются первые {head} и последние {tail} (предел OCR)")

            ocr_texts: dict = {}
            if need_ocr:
                with tempfile.TemporaryDirectory() as temp_dir:
                    res = await asyncio.gather(*(process_page(i, temp_dir) for i in need_ocr),
                                               return_exceptions=True)
                ocr_texts = dict(zip(need_ocr, res))

            results = []
            for i in range(total):
                if i in over_limit:
                    results.append(f"Page {i+1}:\nСтраница {i+1}: [не распознана — предел OCR {limit} страниц]")
                elif i not in ocr_texts:
                    results.append(f"Page {i+1}:\nСтраница {i+1}: {layer[i][0].strip()}")
                elif isinstance(ocr_texts[i], Exception):
                    results.append(f"Страница {i+1} (OCR): [Ошибка: {ocr_texts[i]}]")
                else:
                    results.append(f'Page {i+1}:\n{ocr_texts[i]}')
            logger.info(f"PDF {original_filename}: страниц {total}, текстовый слой "
                        f"{total - len(need_ocr) - len(over_limit)}, OCR {len(need_ocr)}"
                        + (f", не распознано по пределу {len(over_limit)}" if over_limit else ""))

            result = "\n\n".join(results)
            return result.strip() if result.strip() else "[Нет читаемого текста]"

        except BrokenFile:
            raise
        except Exception as e:
            raise ValueError(f"Ошибка при обработке {original_filename} через OCR: {str(e)}")


    async def read_excel_file(self, file_path: str, ext: str) -> str:
        """
        Читает содержимое Excel-файла (.xls или .xlsx) и преобразует его в текстовый формат.

        .xlsx читается потоково (:func:`xlsx_text_limited`, openpyxl ``read_only``) с ограничениями строк, столбцов,
        листов и символов (``EXCEL_MAX_*``): память и время не зависят от размеченного размера листа. .xls — через
        `openpyxl`, затем `xlrd` (не больше ``EXCEL_MAX_ROWS`` строк на лист), при неудаче — LibreOffice.

        Args:
            file_path (str): Путь к Excel-файлу на диске.

        Raises:
            ValueError: Если файл не существует или имеет неподдерживаемое расширение.
            RuntimeError: Если все попытки чтения файла завершились ошибкой.
            ValueError: Если указан неверный путь к файлу.
            RuntimeError: Если возникла ошибка при обработке данных Excel.

        Returns:
            str: Текстовое представление всех листов Excel-файла, включая:
                - название каждого листа,
                - табличные данные в читаемом формате (ограничено 20 строками на лист),
                - очищенные от NaN значения.
                Отсутствующие ячейки заменяются пустыми строками, технические названия колонок (например, 'Unnamed') переименовываются.
                В случае ошибки чтения — вызывается исключение.
        """
        if not os.path.exists(file_path):
            raise ValueError(f"Файл не найден: {file_path}")
        limits = excel_limits()
        try:
            if ext == '.xlsx':
                # Потоковое чтение с ограничением строк и столбцов: полная загрузка pandas/openpyxl книги-сметы
                # (2 МБ, лист размечен до 1 048 576 строк) занимала гигабайты памяти и подвешивала прогон
                # (РМ 31.10.2026: три сметы ТГУ одновременно — воркер завис на 1,5 ч без логов)
                full_text = await asyncio.to_thread(xlsx_text_limited, file_path, **limits)
                if not full_text:
                    raise ValueError("в книге нет листов с текстом")
            elif ext == '.xls':
                try:
                    excel_data = await asyncio.to_thread(
                        pd.read_excel, file_path, sheet_name=None, engine="openpyxl", nrows=limits["max_rows"])
                except Exception as openpyxl_error:
                    logger.warning(f"openpyxl не смог прочитать .xls файл: {openpyxl_error}. Пробуем xlrd...")
                    try:
                        excel_data = await asyncio.to_thread(
                            pd.read_excel, file_path, sheet_name=None, engine='xlrd', nrows=limits["max_rows"])
                    except Exception as xlrd_error:
                        logger.warning(f"xlrd также не сработал: {xlrd_error}. Пробуем LibreOffice...")
                        return await self.read_excel_with_libreoffice(file_path)
                full_text = await asyncio.to_thread(_frames_text, excel_data)
            else:
                raise ValueError("Поддерживаются только файлы .xls и .xlsx")
            logger.info(f"Успешно извлечен текст из Excel: {len(full_text)} символов")
            return full_text

        except Exception as e:
            logger.error(f"Критическая ошибка при обработке Excel файла: {str(e)}")
            raise RuntimeError(f"Не удалось прочитать Excel файл: {str(e)}")


    async def read_excel_with_libreoffice(self, file_path: str) -> str:
        """
        Читает Excel-файл (.xls и др.) с помощью LibreOffice, конвертируя его в .xlsx и извлекая текстовое содержимое.

        Является резервным методом для обработки старых или повреждённых Excel-файлов, которые не удаётся прочитать
        стандартными библиотеками (например, `xlrd` или `openpyxl`). Запускает LibreOffice в headless-режиме,
        конвертирует файл во временный формат .xlsx, затем читает его с помощью pandas и преобразует в строку.

        Args:
            file_path (str): Путь к исходному Excel-файлу (обычно .xls), который необходимо обработать.

        Raises:
            RuntimeError: Если LibreOffice не установлен или недоступен в системе.
            RuntimeError: Если команда конвертации завершилась с ошибкой.
            RuntimeError: Если после конвертации не найден выходной .xlsx-файл.
            RuntimeError: Если произошла ошибка при чтении сконвертированного файла.
            RuntimeError: Если возникла любая другая ошибка на этапе обработки (общее исключение).

        Returns:
            str: Текстовое представление всех листов файла, включая:
                - название каждого листа,
                - строки с ячейками через « | » (не больше ``EXCEL_MAX_ROWS`` строк на лист, см. ``xlsx_text_limited``).
                При ошибке — выбрасывает исключение с детализацией проблемы.
        """
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp()
            temp_xlsx_path = os.path.join(temp_dir, "converted.xlsx")
            
            try:
                await run_soffice(['--convert-to', 'xlsx:Calc MS Excel 2007 XML', '--outdir', temp_dir, file_path])
            except FileNotFoundError:
                raise RuntimeError("LibreOffice не доступен")
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Конвертация дольше {SOFFICE_TIMEOUT} с")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Ошибка конвертации: {e.stderr}")
            
            # Ищем сконвертированный файл
            converted_files = [f for f in os.listdir(temp_dir) if f.endswith('.xlsx')]
            if not converted_files:
                raise RuntimeError("Не удалось найти сконвертированный файл")
            
            temp_xlsx_path = os.path.join(temp_dir, converted_files[0])
            # потоково и с ограничениями, как .xlsx (полная загрузка pandas подвешивала прогон на больших книгах)
            return await asyncio.to_thread(xlsx_text_limited, temp_xlsx_path, **excel_limits())
            
        except Exception as e:
            logger.error(f"Ошибка конвертации через LibreOffice: {str(e)}")
            raise RuntimeError(f"Не удалось обработать Excel файл даже через LibreOffice: {str(e)}")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as cleanup_error:
                    logger.warning(f"Ошибка при очистке временных файлов: {cleanup_error}")


    async def extract_text_and_images_from_docx(self, file_path: str, original_filename:str, ocr_func=None) -> str:
        """
        Извлекает текст и распознаёт текст с изображений из файла DOCX.

        Функция извлекает текст из абзацев и таблиц документа, а также обрабатывает встроенные изображения,
        извлекая их из ZIP-структуры DOCX. Для каждого изображения можно выполнить OCR с помощью переданной
        функции `ocr_func`. Результаты объединяются в единый текстовый вывод.

        Args:
            file_path (str): Путь к файлу .docx.
            ocr_func (callable, optional): Функция для распознавания текста на изображении. 
                                        Должна принимать путь к изображению и возвращать строку с текстом.
                                        Если не указана, изображения помечаются без распознавания.
                                        Defaults to None.

        Raises:
            ValueError: Если произошла ошибка при обработке файла DOCX.

        Returns:
            str: Объединённый текст, содержащий:
                - обычный текст из документа;
                - результаты OCR с изображений (если `ocr_func` указана) или метки изображений.
                Все элементы разделены переносами строк. В случае ошибки — исключение.
        """
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                doc = await asyncio.to_thread(Document, file_path)
                result = []
                for para in doc.paragraphs:
                    if para.text.strip():
                        result.append(para.text)
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            if cell.text.strip():
                                result.append(cell.text)

                image_texts = []
                doc_zip = await asyncio.to_thread(zipfile.ZipFile, file_path)
                image_files = [name for name in doc_zip.namelist() if name.startswith('word/media/')]
                for img_file in image_files:
                    try:
                        if img_file.lower().endswith(('.wmf', '.emf')):
                            logger.info(f"Пропускаем неподдерживаемый формат изображения: {img_file}")
                            continue
                        img_data = doc_zip.read(img_file)
                        img_path = os.path.join(temp_dir, os.path.basename(img_file))
                        async with aiofiles.open(img_path, 'wb') as f:
                            await f.write(img_data)
                        if ocr_func:
                            ocr_text = await ocr_func(img_path, original_filename)
                            image_texts.append(f"[OCR из изображения {os.path.basename(img_file)}]: {ocr_text}")
                        else:
                            image_texts.append(f"[Изображение: {os.path.basename(img_file)}]")
                    except Exception as e:
                        logger.error(f"Ошибка обработки изображения {img_file}: {str(e)}")
                        continue

                if image_texts:
                    result.append("\n".join(image_texts))
                return "\n".join(result)

        except Exception as e:
            logger.error(f"Ошибка обработки DOCX: {str(e)}")
            raise ValueError(f"Ошибка обработки DOCX: {str(e)}")
        

    async def extract_archive(self, archive_path: str, ext: str, output_dir: str) -> Dict[str, str]:
        """Извлекает архив любого поддерживаемого типа.

        zip, rar и tar распаковываются по одному файлу с безопасными именами (:func:`safe_member_path`): имя длиннее
        предела файловой системы (255 байт; кириллица — 2 байта на символ) сокращается — 20.11.2026 архив
        с файлом «33. Аппаратно-программный комплекс … с виртуальным пациентом.pdf» не читался целиком (Errno 36);
        пути с «..» и абсолютные не выходят за ``output_dir``; русские имена zip без флага UTF-8 — в cp866.

        Args:
            archive_path: Путь к архиву.
            ext: Расширение архива.
            output_dir: Каталог распаковки.

        Returns:
            Dict[str, str]: Сохранённое имя (относительно ``output_dir``) → исходное имя в архиве, для файлов,
            имя которых пришлось изменить.
        """
        renamed: Dict[str, str] = {}

        def _save(name: str, open_member) -> None:
            """Записывает член архива под безопасным именем."""
            rel = safe_member_path(name, output_dir)
            if not rel:
                return
            dst = os.path.join(output_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open_member() as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            original = name.replace("\\", "/").lstrip("/")
            if rel.replace(os.sep, "/") != original:
                renamed[rel] = original

        def _extract():

            if ext == ".zip":
                with zipfile.ZipFile(archive_path) as archive:
                    for info in archive.infolist():
                        if not info.is_dir():
                            name = info.filename if info.flag_bits & 0x800 else zip_name(info.filename)
                            _save(name, lambda i=info: archive.open(i))

            elif ext == ".rar":
                with rarfile.RarFile(archive_path) as archive:
                    for info in archive.infolist():
                        if not info.is_dir():
                            _save(info.filename, lambda i=info: archive.open(i))

            elif ext == ".7z":
                with py7zr.SevenZipFile(archive_path, "r") as archive:
                    archive.extractall(output_dir)

            elif ext in [
                ".tar",
                ".tar.gz",
                ".tgz",
                ".tar.bz2",
                ".tbz2",
                ".tar.xz",
                ".txz",
            ]:
                with tarfile.open(archive_path, "r:*") as archive:
                    for member in archive.getmembers():
                        if member.isfile():
                            _save(member.name, lambda m=member: archive.extractfile(m))

            elif ext == ".gz":
                filename = os.path.basename(archive_path[:-3])

                out_file = os.path.join(output_dir, filename)

                with gzip.open(archive_path, "rb") as fin:
                    with open(out_file, "wb") as fout:
                        shutil.copyfileobj(fin, fout)

            elif ext == ".bz2":

                filename = os.path.basename(archive_path[:-4])

                out_file = os.path.join(output_dir, filename)

                with bz2.open(archive_path, "rb") as fin:
                    with open(out_file, "wb") as fout:
                        shutil.copyfileobj(fin, fout)

            elif ext == ".xz":

                filename = os.path.basename(archive_path[:-3])

                out_file = os.path.join(output_dir, filename)

                with lzma.open(archive_path, "rb") as fin:
                    with open(out_file, "wb") as fout:
                        shutil.copyfileobj(fin, fout)

            else:
                raise ValueError(f"Архив {ext} не поддерживается")

        await asyncio.to_thread(_extract)
        return renamed


    async def process_archive(self, file_path: str, ext: str) -> str:
        """Извлекает архив и читает вложенные документы.

        Служебные файлы (временные файлы Word ``~$*.docx``, ``Thumbs.db``, ``__MACOSX`` …) пропускаются всегда:
        это не документы, а их чтение давало ошибки с трассировкой (18.11.2026, архив проектной документации).

        Без ``self.archive_limits`` (экспертиза) читаются все остальные файлы, как раньше — параллельно. С пределами
        (риск-мониторинг): файлы по очереди «документы → таблицы → прочее → картинки», не больше ``max_files``
        файлов и ``max_images`` картинок (OCR), пока текст не превысит ``max_chars``; по ``concurrency`` файлов
        одновременно. Непрочитанные файлы перечисляются в конце текста именами.

        Args:
            file_path: Путь к архиву.
            ext: Расширение архива.

        Returns:
            str: Тексты файлов с заголовками ``=== имя ===``.
        """

        async def process_single_file(file_path, relative_name):
            try:
                text = await self.read_file(
                    file_path,
                    original_filename=relative_name
                )
                return f"=== {relative_name} ===\n{text}"

            except Exception as e:
                return f"=== {relative_name} ===\n[Ошибка чтения: {e}]"

        limits = self.archive_limits
        with tempfile.TemporaryDirectory() as tmpdir:

            renamed = await self.extract_archive(file_path, ext, tmpdir) or {}

            entries = []   # (полный путь, имя внутри архива)
            not_text = []  # файлы форматов без текста — только имена
            skipped_junk = 0
            for root, _, files in os.walk(tmpdir):
                for filename in files:
                    full_path = os.path.join(root, filename)
                    relative_name = os.path.relpath(full_path, tmpdir)
                    relative_name = renamed.get(relative_name, relative_name)   # исходное имя, если сокращено
                    if is_archive_junk(relative_name):
                        skipped_junk += 1
                        continue
                    if not is_readable_name(relative_name):
                        not_text.append(relative_name)   # чертежи .dwg, .tmp и пр.: read_file их не читает
                        continue
                    entries.append((full_path, relative_name))
            if skipped_junk:
                logger.info(f"Архив {os.path.basename(file_path)}: пропущено служебных файлов {skipped_junk}")

            if not_text:
                logger.info(f"Архив {os.path.basename(file_path)}: нетекстовых файлов {len(not_text)} "
                            f"({', '.join(sorted({os.path.splitext(n)[1].lower() or '?' for n in not_text}))}) — "
                            f"перечислены по именам")
            if not limits:
                results = list(await asyncio.gather(*(process_single_file(f, n) for f, n in entries)))
                skipped, total = [], 0
            else:
                results, skipped, total = await self._read_archive_limited(entries, limits, process_single_file)
        if not_text:
            shown = sorted(not_text)[:100]
            more = f"\nи ещё {len(not_text) - len(shown)}" if len(not_text) > len(shown) else ""
            results.append("=== Нетекстовые файлы (чертежи, служебные и пр. — не читались) ===\n"
                           + "\n".join(shown) + more)
        if not limits:
            return "\n\n".join(results)

        if skipped:
            shown = skipped[:100]
            more = f" и ещё {len(skipped) - len(shown)}" if len(skipped) > len(shown) else ""
            results.append(f"=== Не прочитано (предел чтения архива: прочитано {len(entries) - len(skipped)} из "
                           f"{len(entries)} файлов, {total} символов) ===\n" + "\n".join(n for _, n in shown) + more)
            logger.info(f"Архив {os.path.basename(file_path)}: прочитано {len(entries) - len(skipped)} из "
                        f"{len(entries)} файлов ({total} символов), остальные — по пределам архива")
        return "\n\n".join(results)

    @staticmethod
    async def _read_archive_limited(entries, limits, read_one):
        """Читает файлы архива по очереди приоритета до пределов (см. :meth:`process_archive`).

        Args:
            entries: ``[(путь, имя в архиве)]``.
            limits: ``max_files``, ``max_images``, ``max_chars``, ``concurrency``.
            read_one: Корутина ``(путь, имя) -> текст``.

        Returns:
            tuple: ``(тексты, непрочитанные [(путь, имя)], символов прочитано)``.
        """
        max_files = int(limits.get("max_files") or len(entries) or 1)
        max_images = int(limits.get("max_images", max_files))
        max_chars = int(limits.get("max_chars") or 0)
        step = max(1, int(limits.get("concurrency") or 4))
        ordered = sorted(entries, key=lambda e: (archive_entry_rank(e[1]), e[1]))
        chosen, images = [], 0
        for e in ordered:
            if len(chosen) >= max_files:
                break
            if archive_entry_rank(e[1]) == ARCHIVE_RANK_IMAGE:
                if images >= max_images:
                    continue
                images += 1
            chosen.append(e)
        results, total, done = [], 0, 0
        while done < len(chosen) and not (max_chars and total >= max_chars):
            batch = chosen[done:done + step]
            texts = await asyncio.gather(*(read_one(f, n) for f, n in batch))
            results.extend(texts)
            total += sum(len(t) for t in texts)
            done += len(batch)
        read = {n for _, n in chosen[:done]}
        skipped = [e for e in ordered if e[1] not in read]
        return results, skipped, total


    async def read_html(self, html_path: str) -> str:

        async with aiofiles.open(html_path, 'r', encoding='utf-8') as f:
            html_content = await f.read()

        soup = BeautifulSoup(html_content, 'html.parser')

        # Удаляем служебные блоки, если они есть
        for tag in soup(['script', 'style', 'meta', 'link']):
            tag.decompose()

        # Заменяем <br> и <p> на переносы строк
        for br in soup.find_all('br'):
            br.replace_with('\n')

        # Извлекаем текст, убираем лишние пробелы и пустые строки
        text = soup.get_text(separator='\n', strip=True)
        clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
        
        return '\n'.join(clean_lines)


    async def html_to_pdf(self, html_path: str, original_filename:str) -> str:
        """Асинхронно конвертирует HTML-файл в PDF с помощью wkhtmltopdf"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            pdf_path = tmp.name

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "wkhtmltopdf",

                    # 🔥 ключевые настройки
                    "--print-media-type",          # использовать @media print
                    "--enable-local-file-access",  # доступ к локальным ресурсам
                    "--encoding", "utf-8",

                    # 📄 формат страницы
                    "--page-size", "A4",
                    "--margin-top", "10mm",
                    "--margin-bottom", "10mm",
                    "--margin-left", "10mm",
                    "--margin-right", "10mm",

                    # ⚙️ стабильность
                    "--load-error-handling", "ignore",
                    "--load-media-error-handling", "ignore",
                    # "--no-images",
                    # "--disable-javascript",

                    html_path,
                    pdf_path
                ],
                capture_output=True,
                text=True,
                timeout=30)
            
            if result.returncode != 0:
                raise RuntimeError(f"wkhtmltopdf failed: {result.stderr}")
            
            return await self.read_pdf_file(pdf_path, original_filename)
        
        except Exception as e:
            logger.error(f"Ошибка конвертации HTML → PDF для {original_filename}: {e}")
            raise


    async def read_xml_file(self, file_path: str) -> str:
        """
        Асинхронно читает XML-файл и преобразует в плоский текст.
        Безопасно для внешних XML.
        """

        def _parse_xml():
            try:
                lines = []

                for event, elem in ET.iterparse(file_path, events=("end",)):
                    tag = elem.tag.split('}')[-1]
                    text = (elem.text or "").strip()

                    if text:
                        lines.append(f"{tag}: {text}")

                    elem.clear()

                return "\n".join(lines)

            except Exception as e:
                logger.warning(f"Не удалось распарсить XML: {e}")

                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        return f.read()
                except Exception:
                    return ""

        return await asyncio.to_thread(_parse_xml)


    def get_extension(self, filename: str) -> str:
        filename = filename.lower()

        for ext in (
            ".tar.gz",
            ".tar.bz2",
            ".tar.xz",
            ".tgz",
            ".tbz2",
            ".txz",
        ):
            if filename.endswith(ext):
                return ext

        return os.path.splitext(filename)[1]


    async def read_file(self, file_path: str, original_filename: str = None) -> str:
        """
        Читает файл и извлекает текстовое содержимое с улучшенной обработкой бинарных файлов

        Args:
            file_path (str): Путь к файлу на диске.
            original_filename (str, optional): Оригинальное имя файла (может отличаться от имени во временном хранилище).
                                            Используется для определения расширения. Defaults to None.

        Raises:
            ValueError: Если произошла ошибка при чтении файла (например, повреждён, недоступен).

        Returns:
            str: Извлечённый текст или сообщение о типе файла, если формат не поддерживается.
                Для неподдерживаемых бинарных форматов возвращается строка вида "Бинарный файл ...".
        """
        ext = self.get_extension(original_filename)
        logger.info(f"Чтение файла: {original_filename} (расширение: {ext})")

        try:
            # Если расширение .bin, пробуем определить реальный тип
            if ext == ".bin":
                real_extension = await self.detect_binary_file_type(file_path)
                logger.info(f"Определен реальный тип бинарного файла: {real_extension}")
                
                # Создаем копию файла с правильным расширением для обработки
                if real_extension != ".bin":
                    new_path = file_path + real_extension
                    shutil.copy2(file_path, new_path)
                    try:
                        result = await self.read_file(new_path, original_filename + real_extension)
                        os.unlink(new_path)
                        return result
                    except Exception as e:
                        logger.warning(f"Не удалось обработать файл с определенным расширением {real_extension}: {str(e)}")
                        os.unlink(new_path)
                        # Продолжаем с оригинальным .bin файлом

            # Основная логика обработки по расширениям
            if ext in [".txt", ".csv", ".json"]:
                text = await self.read_txt_file(file_path)
                logger.info(f"Прочитан {ext.upper()}-файл: {original_filename}, длина: {len(text)}")
                return text 
            elif ext == ".pdf":
                text = await self.read_pdf_file(file_path, original_filename)
                logger.info(f"Извлечён текст из PDF: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".xlsx", ".xls"]:
                text = await self.read_excel_file(file_path, ext)
                logger.info(f"Извлечён текст из Excel: {original_filename}, длина: {len(text)}")
                return text
            elif ext == ".pptx":
                text = await self.read_pptx_file(file_path)
                logger.info(f"Извлечён текст из PPTX: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".doc", ".docx", '.odt', '.rtf']:
                text = await self.convert_to_pdf(file_path, ext, original_filename)
                logger.info(f"Извлечён текст из DOC(X): {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".gz", ".bz2", ".xz"]:
                text = await self.process_archive(file_path, ext)
                logger.info(f"Извлечён текст из архива: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".html"]:
                text = await self.read_html(file_path)
                logger.info(f"Извлечён текст из HTML: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".xml"]:
                text = await self.read_xml_file(file_path)
                logger.info(f"Извлечён текст из XML: {original_filename}, длина: {len(text)}")
                return text
            elif ext in [".sig"]:
                return "Документ заверен электронной подписью."
            elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
                # Обработка изображений через OCR
                try:
                    text = await self.ocr_processor.ocr_image_with_qwen_vl(file_path, original_filename)
                    logger.info(f"Извлечён текст из изображения через OCR: {original_filename}, длина: {len(text)}")
                    return text
                except Exception as e:
                    logger.warning(f"Не удалось извлечь текст из изображения {original_filename}: {str(e)}")
                    return f"Изображение {os.path.basename(file_path)} (текст не распознан)"
            else:
                # Для неизвестных форматов пробуем определить тип и обработать
                if ext == ".bin":
                    # Уже пробовали определить тип выше, если дошли сюда - не удалось
                    msg = f"Бинарный файл {os.path.basename(file_path)} (не удалось определить формат)"
                else:
                    msg = f"Бинарный файл {os.path.basename(file_path)} (формат {ext})"
                
                logger.warning(f"Неподдерживаемый формат файла: {original_filename}")
                return msg
                
        except BrokenFile as e:
            # повреждённый файл — без трассировки: причина в файле, а не в коде
            logger.warning(f"Файл {original_filename} повреждён: {e}")
            raise BrokenFile(f"Ошибка чтения файла: {e}") from e
        except Exception as e:
            logger.error(f"Ошибка чтения файла {original_filename}: {str(e)}", exc_info=True)
            raise ValueError(f"Ошибка чтения файла: {str(e)}")


    async def detect_binary_file_type(self, file_path: str) -> str:
        """
        Определяет тип бинарного файла по его содержимому (сигнатурам)
        
        Args:
            file_path (str): Путь к файлу
            
        Returns:
            str: Расширение файла (.pdf, .docx, .jpg и т.д.) или .bin если не удалось определить
        """
        try:
            async with aiofiles.open(file_path, 'rb') as f:
                header = await f.read(12)  # Читаем первые 12 байт для лучшего определения
            # PDF - %PDF
            if header.startswith(b'%PDF'):
                return '.pdf'
            # ZIP-based formats (DOCX, XLSX, PPTX, ODT и т.д.)
            if header.startswith(b'PK\x03\x04'):
                # Можно попробовать определить точный тип по структуре ZIP
                try:
                    with await asyncio.to_thread(zipfile.ZipFile, file_path, 'r') as zip_file:
                        namelist = zip_file.namelist()
                        # Проверяем структуру для разных форматов
                        if any(name.startswith('word/') for name in namelist):
                            return '.docx'
                        elif any(name.startswith('xl/') for name in namelist):
                            return '.xlsx'
                        elif any(name.startswith('ppt/') for name in namelist):
                            return '.pptx'
                        else:
                            return '.zip'  # обычный ZIP архив
                except:
                    return '.docx'  # по умолчанию считаем DOCX
            # Microsoft Office old formats (DOC, XLS, PPT)
            if header.startswith(b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'):
                return '.doc'  # по умолчанию DOC
            # JPEG
            if header.startswith(b'\xFF\xD8\xFF'):
                return '.jpg'
            # PNG
            if header.startswith(b'\x89PNG\r\n\x1a\n'):
                return '.png'
            # GIF
            if header.startswith(b'GIF8'):
                return '.gif'
            # BMP
            if header.startswith(b'BM'):
                return '.bmp'
            # TIFF
            if header.startswith(b'II\x2A\x00') or header.startswith(b'MM\x00\x2A'):
                return '.tiff'
            # RAR
            if header.startswith(b'Rar!\x1A\x07\x00') or header.startswith(b'Rar!\x1A\x07\x01'):
                return '.rar'
            # 7Z
            if header.startswith(b'7z\xBC\xAF\x27\x1C'):
                return '.7z'
            # Microsoft Cabinet (CAB)
            if header.startswith(b'MSCF'):
                return '.cab'
            # Windows Executable
            if header.startswith(b'MZ'):
                return '.exe'
            # UTF-8/16 text files with BOM
            if header.startswith(b'\xEF\xBB\xBF'):  # UTF-8 BOM
                return '.txt'
            if header.startswith(b'\xFF\xFE') or header.startswith(b'\xFE\xFF'):  # UTF-16 BOM
                return '.txt'
            # gzip
            if header.startswith(b"\x1f\x8b"):
                return ".gz"
            # bzip2
            if header.startswith(b"BZh"):
                return ".bz2"
            # xz
            if header.startswith(b"\xfd7zXZ\x00"):
                return ".xz"
            # tar
            try:
                with open(file_path, "rb") as f:
                    f.seek(257)
                    if f.read(5) == b"ustar":
                        return ".tar"
            except Exception:
                pass
            # Пробуем определить как текстовый файл
            try:
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    await f.read(1024)  # Пробуем прочитать как текст
                return '.txt'
            except:
                pass
                
            return '.bin'
            
        except Exception as e:
            logger.error(f"Ошибка определения типа бинарного файла {file_path}: {str(e)}")
            return '.bin'
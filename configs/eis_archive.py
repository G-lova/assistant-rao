"""Архивы ЕИС на диске: скачивание потоком во временный файл и распаковка без чтения в память целиком.

Раньше ``_download_eis_archive`` (``configs/parsing.py``, ``configs/eis_parsing.py``) читал архив целиком в память
(``response.read()``), распаковывал каждый файл тоже целиком (``zip_file.read``), а в ``eis_parsing`` ещё и
кодировал каждый файл в base64 для ответа. Архив закупки с чертежами и сметами — сотни мегабайт, и в памяти
оказывались архив, файлы и их base64-копии одновременно. Распаковка к тому же шла в цикле событий.

Здесь:

* :func:`download_to_tempfile` пишет ответ на диск порциями;
* :func:`extract_zip` распаковывает каждый файл архива потоком (``shutil.copyfileobj``) в свой временный файл;
  расширение без имени определяется по первым байтам. Функция синхронная — вызывать через ``asyncio.to_thread``.
"""
import os
import re
import shutil
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

CHUNK_SIZE = 256 * 1024
SNIFF_BYTES = 8192        # столько первых байт файла достаточно для python-magic


async def download_to_tempfile(response: Any, suffix: str = ".zip") -> Tuple[str, int]:
    """Сохраняет тело ответа ``aiohttp`` во временный файл порциями.

    Args:
        response: Ответ ``aiohttp`` (``response.content.iter_chunked``).
        suffix: Расширение временного файла.

    Returns:
        Tuple[str, int]: Путь к файлу и его размер в байтах. При ошибке чтения файл удаляется.
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                f.write(chunk)
                size += len(chunk)
    except BaseException:
        remove_quietly(path)
        raise
    return path, size


def remove_quietly(path: Optional[str]) -> None:
    """Удаляет файл, если он есть; ошибки игнорируются."""
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def safe_filename(name: str) -> str:
    """Имя файла без символов, недопустимых в путях (``<>:"/\\|?*`` → ``_``)."""
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def extract_zip(archive_path: str, original_url: str, procurement_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Распаковывает ZIP во временные файлы — по одному на файл архива, потоком.

    Формат элементов — как у прежнего ``_process_eis_archive``: ``status``, ``filename`` (безопасное имя),
    ``file_path``, ``source = "eis_soap"``, ``original_url``, ``procurement_id``, ``file_extension``.

    Args:
        archive_path: Путь к ZIP.
        original_url: Ссылка на архив (для результата).
        procurement_id: ID экспертизы (для результата).

    Returns:
        List[Dict[str, Any]]: Файлы архива (папки пропускаются).

    Raises:
        zipfile.BadZipFile: Файл не ZIP. Уже распакованные файлы при ошибке удаляются.
    """
    from configs.utils import get_file_extension

    files: List[Dict[str, Any]] = []
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                _, ext = os.path.splitext(info.filename)
                with zf.open(info) as src:
                    if not ext:
                        head = src.read(SNIFF_BYTES)
                        ext = get_file_extension(content=head)
                    else:
                        head = b""
                    fd, tmp_path = tempfile.mkstemp(suffix=ext)
                    with os.fdopen(fd, "wb") as dst:
                        dst.write(head)
                        shutil.copyfileobj(src, dst, CHUNK_SIZE)
                files.append({
                    "status": "success",
                    "filename": safe_filename(info.filename),
                    "file_path": tmp_path,
                    "source": "eis_soap",
                    "original_url": original_url,
                    "procurement_id": procurement_id,
                    "file_extension": ext,
                })
    except BaseException:
        for f in files:
            remove_quietly(f["file_path"])
        raise
    return files

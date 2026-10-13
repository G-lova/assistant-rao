"""Общий документ по содержимому (``ai_documents``): один текст, один набор чанков и кэш LLM на SHA-256 файла.

Экспертизы (``pe_documents``) и риск-мониторинг (``ai_file_links``) хранят только ссылки на документ.
Один и тот же файл — например, извещение из ЕИС — распознаётся один раз, даже если он пришёл и в
риск-мониторинг, и в экспертизу, и повторно в ту же экспертизу.

Функции принимают открытое соединение ``asyncpg`` (или совместимый объект) и флагов не проверяют —
это делает :mod:`knowledge_store.hooks`.
"""
import hashlib
import os
from typing import Optional

READER_FILE = "file_reader"
"""Текст получен ``configs.file_reader.FileReader`` (``/evaluate-documents``, РМ для не-PDF)."""
READER_RM_PDF = "rm_pdf_text"
"""Текст получен ``risk_monitoring.pdf_text`` (текстовый слой PDF + OCR только сканов)."""

SQL_FIND_BY_SHA = """
SELECT id, text, ocr_status, reader, text_purged_at FROM ai_documents WHERE content_sha256 = $1
"""

# Текст документа заменяется, только если его ещё нет, он не прошёл контроль качества (ocr_status <> 'done')
# или это принудительное перечитывание тем же способом ($8). CTE возвращает id и тогда, когда обновления нет.
SQL_SAVE_TEXT = """
WITH ins AS (
    INSERT INTO ai_documents (content_sha256, text_sha256, file_ext, size_bytes, text, ocr_status, reader, pages_count)
    VALUES ($1, $2, $3, $4, $5, 'done', $6, $7)
    ON CONFLICT (content_sha256) DO UPDATE SET
        text = EXCLUDED.text, text_sha256 = EXCLUDED.text_sha256, ocr_status = 'done', ocr_error = NULL,
        reader = EXCLUDED.reader, pages_count = EXCLUDED.pages_count, text_purged_at = NULL, updated_at = NOW()
    WHERE ai_documents.text IS NULL OR ai_documents.ocr_status <> 'done'
       OR ($8 AND ai_documents.reader IS NOT DISTINCT FROM EXCLUDED.reader)
    RETURNING id
)
SELECT id FROM ins
UNION ALL
SELECT id FROM ai_documents WHERE content_sha256 = $1
LIMIT 1
"""


def file_sha256(path: str) -> str:
    """SHA-256 содержимого файла (блоками, без загрузки целиком в память).

    Args:
        path: Путь к файлу.

    Returns:
        str: Хэш в hex (64 символа) — ключ ``ai_documents.content_sha256``.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def text_sha256(text: str) -> str:
    """SHA-256 извлечённого текста (поиск точных дублей между форматами).

    Args:
        text: Текст документа.

    Returns:
        str: Хэш в hex.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def find_text(conn, content_sha256: str) -> Optional[dict]:
    """Готовый текст документа по хэшу файла, если его можно использовать повторно.

    Повторно используется только текст, прошедший контроль качества (``ocr_status = 'done'``) и не
    очищенный политикой хранения. Текст со статусом ``suspect`` (циклы OCR) перечитывается.

    Args:
        conn: Соединение ``asyncpg``.
        content_sha256: SHA-256 файла.

    Returns:
        Optional[dict]: ``{"id", "text", "reader"}`` или ``None``.
    """
    rows = await conn.fetch(SQL_FIND_BY_SHA, content_sha256)
    if not rows:
        return None
    row = dict(rows[0])
    if not row.get("text") or row.get("ocr_status") != "done":
        return None
    return {"id": int(row["id"]), "text": row["text"], "reader": row.get("reader")}


async def save_text(conn, content_sha256: str, file_path: Optional[str], filename: Optional[str], text: str,
                    reader: str, pages_count: int, overwrite: bool = False) -> int:
    """Создаёт документ с текстом или возвращает существующий.

    Если документ уже есть и у него годный текст, текст не меняется (первое чтение «выигрывает», чтобы
    анализы РМ и экспертизы опирались на один текст); при ``overwrite`` перечитывание тем же способом
    заменяет текст.

    Args:
        conn: Соединение ``asyncpg``.
        content_sha256: SHA-256 файла.
        file_path: Локальный путь (для размера), может отсутствовать.
        filename: Имя файла (для расширения).
        text: Извлечённый текст.
        reader: Способ чтения (:data:`READER_FILE` / :data:`READER_RM_PDF`).
        pages_count: Число страниц.
        overwrite: Принудительное перечитывание (``force``).

    Returns:
        int: ``ai_documents.id``.
    """
    ext = os.path.splitext(filename or file_path or "")[1].lower() or None
    size = os.path.getsize(file_path) if file_path and os.path.exists(file_path) else None
    return int(await conn.fetchval(SQL_SAVE_TEXT, content_sha256, text_sha256(text), ext, size, text, reader,
                                   int(pages_count), bool(overwrite)))


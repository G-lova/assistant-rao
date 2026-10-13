"""Общий документ по содержимому (``ai_documents``): один текст, один набор чанков и кэш LLM на SHA-256 файла.

Экспертизы (``pe_documents``) и риск-мониторинг (``ai_file_links``) хранят только ссылки на документ.
Один и тот же файл — например, извещение из ЕИС — распознаётся один раз, даже если он пришёл и в
риск-мониторинг, и в экспертизу, и повторно в ту же экспертизу.

Функции принимают открытое соединение ``asyncpg`` (или совместимый объект) и флагов не проверяют —
это делает :mod:`knowledge_store.hooks`.
"""
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

READER_FILE = "file_reader"
"""Текст получен ``configs.file_reader.FileReader`` (``/evaluate-documents``, РМ для не-PDF)."""
READER_RM_PDF = "rm_pdf_text"
"""Текст получен ``risk_monitoring.pdf_text`` (текстовый слой PDF + OCR только сканов)."""

ROOT = Path(__file__).resolve().parent.parent
EXTRACTION_ANALYSIS = "type_data_extraction"
"""Тип анализа в ``ai_document_analysis`` для кэша ``TypeDataExtractor`` (``/evaluate-documents``)."""
EXTRACTION_SOURCES = ("prompts/type_data_extractor_prompt.txt", "schemas/type_data_extractor_schema.json",
                      "evaluate_documents/type_data_extractor.py")
"""Файлы, от которых зависит результат извлечения: правка любого из них — новая версия кэша."""

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



SQL_GET_ANALYSIS = """
SELECT result FROM ai_document_analysis
WHERE document_id = $1 AND analysis_type = $2 AND prompt_version = $3 AND model = $4
"""

SQL_SAVE_ANALYSIS = """
INSERT INTO ai_document_analysis (document_id, analysis_type, prompt_version, model, result, compact, confidence)
VALUES ($1, $2, $3, $4, $5::jsonb, NULL, NULL)
ON CONFLICT (document_id, analysis_type, prompt_version, model) DO UPDATE SET
    result = EXCLUDED.result, created_at = NOW()
"""

SQL_AI_DOCUMENT_OF_LINK = "SELECT ai_document_id FROM pe_documents WHERE id = $1"


@lru_cache(maxsize=1)
def _sources_hash() -> str:
    """Хэш файлов промпта, схемы и кода экстрактора (читается один раз на процесс)."""
    h = hashlib.sha256()
    for rel in EXTRACTION_SOURCES:
        try:
            h.update((ROOT / rel).read_bytes())
        except OSError:
            h.update(rel.encode())
    return h.hexdigest()[:16]


def extraction_version(doc_code: Optional[str], expertise_object: Any, filename: Optional[str],
                       chunk_size: int, text: str) -> str:
    """Версия кэша извлечения: код и промпт экстрактора + контекст вызова + текст.

    ``TypeDataExtractor`` получает тип документа, объект экспертизы, имя файла и текст, нарезанный на блоки
    ``chunk_size`` — результат зависит от всего этого, поэтому всё входит в ключ (текст — хэшем: если текст
    документа перечитан, прежний результат не используется).

    Args:
        doc_code: Код типа документа ссылки (после нормализации).
        expertise_object: Объект экспертизы (5/6/7…).
        filename: Имя файла.
        chunk_size: Размер блока текста для LLM.
        text: Текст, который уходит в экстрактор.

    Returns:
        str: ``<хэш исходников>:<хэш контекста>``.
    """
    ctx = json.dumps([doc_code, str(expertise_object), filename, int(chunk_size), text_sha256(text or "")],
                     ensure_ascii=False)
    return f"{_sources_hash()}:{hashlib.sha256(ctx.encode('utf-8')).hexdigest()[:16]}"


async def ai_document_of_link(conn, link_id: int) -> Optional[int]:
    """``ai_documents.id`` ссылки экспертизы ``pe_documents.id``.

    Args:
        conn: Соединение ``asyncpg``.
        link_id: ``pe_documents.id``.

    Returns:
        Optional[int]: ``ai_documents.id`` или ``None``.
    """
    value = await conn.fetchval(SQL_AI_DOCUMENT_OF_LINK, int(link_id))
    return int(value) if value is not None else None


def _load(value: Any) -> Any:
    """JSONB из драйвера: asyncpg без кодека возвращает строку."""
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


async def get_extraction(conn, document_id: int, version: str, model: str) -> Optional[dict]:
    """Сохранённый результат ``TypeDataExtractor`` для общего документа.

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``ai_documents.id``.
        version: :func:`extraction_version`.
        model: Модель LLM.

    Returns:
        Optional[dict]: Результат извлечения или ``None``.
    """
    rows = await conn.fetch(SQL_GET_ANALYSIS, int(document_id), EXTRACTION_ANALYSIS, version, model or "")
    if not rows:
        return None
    result = _load(dict(rows[0])["result"])
    return result if isinstance(result, dict) else None


def cacheable_extraction(result: Any) -> bool:
    """Можно ли кэшировать результат: только успешное извлечение (не запасной ответ при сбое модели).

    Args:
        result: Ответ ``TypeDataExtractor``.

    Returns:
        bool: ``True`` — тип определён и данные извлечены.
    """
    if not isinstance(result, dict) or not result.get("raw_data"):
        return False
    tc = result.get("type_compliance")
    detected = tc if isinstance(tc, str) else (tc or {}).get("detected_type")
    return bool(detected) and detected != "unknown"


async def save_extraction(conn, document_id: int, version: str, model: str, result: dict) -> None:
    """Сохраняет результат ``TypeDataExtractor`` в кэш общего документа.

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``ai_documents.id``.
        version: :func:`extraction_version`.
        model: Модель LLM.
        result: Результат извлечения.
    """
    await conn.execute(SQL_SAVE_ANALYSIS, int(document_id), EXTRACTION_ANALYSIS, version, model or "",
                       json.dumps(result, ensure_ascii=False, default=str))

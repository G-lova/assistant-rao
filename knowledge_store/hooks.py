"""Точки вставки для /evaluate-documents: тонкие безопасные обёртки над репозиторием.

Каждая функция сама проверяет флаг ``KNOWLEDGE_STORE_ENABLED`` и никогда не бросает исключений,
поэтому в пайплайне достаточно ``await``-вызова без try/except.
"""
from typing import Any, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import repository as repo
from knowledge_store.pages import split_pages
from knowledge_store.safe import is_enabled, safe_call_async

logger = get_logger(__name__)


def _db():
    """Возвращает менеджер соединения с БД (ленивый импорт: ``configs.working_with_db`` тянет asyncpg/psycopg2)."""
    from configs.working_with_db import get_async_db_connection
    return get_async_db_connection()


def _none_if_nan(value: Any) -> Any:
    """Приводит NaN/NaT из pandas к ``None``, numpy-числа — к обычным Python-числам."""
    try:
        if value != value:  # NaN
            return None
    except Exception:  # noqa: BLE001
        pass
    return value.item() if hasattr(value, "item") else value


async def _on_run_start(df) -> None:
    """Реализация :func:`on_run_start` (без защиты)."""
    row = df.iloc[0]
    expertise_id = int(row["id"])
    passport = {k: _none_if_nan(row.get(k)) for k in ("organization", "expertise_object", "expertise_details")}
    async with _db() as conn:
        async with conn.transaction():
            await repo.clear_expertise(conn, expertise_id)
            await repo.save_procurement(conn, expertise_id, _none_if_nan(row.get("law_reference")),
                                        _none_if_nan(row.get("procurement_method")), _none_if_nan(row.get("object")),
                                        _none_if_nan(row.get("checkType2")), passport)


async def on_run_start(df) -> None:
    """Начало прогона: очищает прежние данные экспертизы в ``pe_*`` и сохраняет паспорт закупки.

    Вызывать после нормализации кодов документов и до обработки ссылок.

    Args:
        df: Датафрейм экспертизы из ``evaluate_docs_script.sql``.
    """
    await safe_call_async(_on_run_start, df)


async def _on_document_text(expertise_id: int, doc_code: Optional[str], filename: str, url: Optional[str],
                            file_path: str, text: str) -> int:
    """Реализация :func:`on_document_text` (без защиты)."""
    pages = split_pages(text)
    sha = repo.file_sha256(file_path)
    async with _db() as conn:
        return await repo.upsert_document(conn, expertise_id, doc_code, filename, url, sha, text,
                                          len(pages), Config.PE_TEXT_RETENTION_DAYS)


async def on_document_text(expertise_id: int, doc_code: Optional[str], filename: str, url: Optional[str],
                           file_path: str, text: str) -> Optional[int]:
    """Сохраняет извлечённый текст документа (со сроком хранения по политике).

    Args:
        expertise_id: ID экспертизы.
        doc_code: Код типа документа.
        filename: Имя файла.
        url: Исходная ссылка.
        file_path: Локальный путь к файлу (для контрольной суммы).
        text: Текст из ``FileReader.read_file``.

    Returns:
        Optional[int]: ``pe_documents.id`` или ``None`` (флаг выключен / ошибка).
    """
    return await safe_call_async(_on_document_text, expertise_id, doc_code, filename, url, file_path, text, default=None)


async def _on_document_extracted(document_id: int, extracted: dict) -> None:
    """Реализация :func:`on_document_extracted` (без защиты)."""
    type_compliance = extracted.get("type_compliance")
    detected = type_compliance if isinstance(type_compliance, str) else (type_compliance or {}).get("detected_type")
    async with _db() as conn:
        await repo.set_document_extraction(conn, document_id, detected, extracted.get("readability"), extracted)


async def on_document_extracted(document_id: Optional[int], extracted: Any) -> None:
    """Сохраняет результат ``TypeDataExtractor`` для ранее записанного документа.

    Args:
        document_id: ``pe_documents.id`` из :func:`on_document_text` (``None`` — ничего не делать).
        extracted: Ответ экстрактора (словарь).
    """
    if document_id is None or not isinstance(extracted, dict) or not is_enabled():
        return
    await safe_call_async(_on_document_extracted, document_id, extracted)


def _enqueue_indexing(expertise_id: int) -> None:
    """Ставит в очередь Celery задачу индексации (ленивый импорт, чтобы не создавать циклов)."""
    from celery_app import celery_app
    celery_app.send_task("index_documents_task", args=[int(expertise_id)], queue="evaluation")


async def _on_run_finished(expertise_id: int) -> None:
    """Реализация :func:`on_run_finished` (без защиты)."""
    _enqueue_indexing(expertise_id)


async def on_run_finished(expertise_id: int) -> None:
    """Конец прогона: запускает фоновую индексацию (чанки + эмбеддинги) отдельной задачей Celery.

    Args:
        expertise_id: ID экспертизы.
    """
    await safe_call_async(_on_run_finished, expertise_id)

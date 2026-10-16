"""Точки вставки для /evaluate-documents: тонкие безопасные обёртки над репозиторием.

Каждая функция сама проверяет флаг ``KNOWLEDGE_STORE_ENABLED`` и никогда не бросает исключений,
поэтому в пайплайне достаточно ``await``-вызова без try/except.
"""
import os
from typing import Any, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import db, documents, eis_notice, repository as repo, rm_link
from knowledge_store.pages import split_pages
from knowledge_store.safe import is_enabled, safe_call_async
from knowledge_store.scope import Scope

logger = get_logger(__name__)


def _db():
    """Возвращает менеджер соединения с БД: из пула задачи (:func:`knowledge_store.db.task_pool`) или временный."""
    return db.connection()


def _none_if_nan(value: Any) -> Any:
    """Приводит NaN/NaT из pandas к ``None``, numpy-числа — к обычным Python-числам."""
    try:
        if value != value:  # NaN
            return None
    except Exception:  # noqa: BLE001
        pass
    return value.item() if hasattr(value, "item") else value


async def _on_run_start(df, env: str) -> None:
    """Реализация :func:`on_run_start` (без защиты)."""
    row = df.iloc[0]
    scope = Scope.of(env, row["id"])
    passport = {k: _none_if_nan(row.get(k)) for k in ("organization", "expertise_object", "expertise_details")}
    async with _db() as conn:
        async with conn.transaction():
            await repo.clear_expertise(conn, scope)
            await repo.save_procurement(conn, scope, _none_if_nan(row.get("law_reference")),
                                        _none_if_nan(row.get("procurement_method")), _none_if_nan(row.get("object")),
                                        _none_if_nan(row.get("checkType2")), passport)
        try:
            # номер контракта из карточки экспертизы — для сопоставления с риск-мониторингом; отдельно от
            # транзакции паспорта, чтобы сбой (например, до миграции 007) не отменял запись паспорта
            await rm_link.save_identifiers(conn, scope, contract_number=_none_if_nan(row.get("contract")))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"knowledge_store: номер контракта {scope} не сохранён: {type(e).__name__}: {e}")


async def on_run_start(df, env: str) -> None:
    """Начало прогона: очищает прежние данные экспертизы в ``pe_*`` и сохраняет паспорт закупки.

    Вызывать после нормализации кодов документов и до обработки ссылок.

    Args:
        df: Датафрейм экспертизы из ``evaluate_docs_script.sql``.
        env: Окружение (``X-API-Database``).
    """
    await safe_call_async(_on_run_start, df, env)


def dedup_enabled() -> bool:
    """Включено ли повторное использование текста общего документа (``DOC_DEDUP_ENABLED``).

    Флаг — ``configs/settings.py``; нужен и ``KNOWLEDGE_STORE_ENABLED``.

    Returns:
        bool: ``True``, если готовый текст можно брать из ``ai_documents``.
    """
    from configs import settings
    return is_enabled() and bool(settings.DOC_DEDUP_ENABLED)


async def _find_cached_text(file_path: str) -> Optional[dict]:
    """Реализация :func:`find_cached_text` (без защиты)."""
    sha = documents.file_sha256(file_path)
    async with _db() as conn:
        found = await documents.find_text(conn, sha)
    if found:
        found["sha256"] = sha
    return found


async def find_cached_text(file_path: str, force: bool = False) -> Optional[dict]:
    """Готовый текст файла по SHA-256 содержимого — до чтения и OCR.

    Ничего не делает (``None``), если выключено хранилище знаний или ``DOC_DEDUP_ENABLED``, а также при
    ``force`` (принудительное перечитывание). Любая ошибка — тоже ``None``: файл будет прочитан как обычно.

    Args:
        file_path: Локальный путь к скачанному файлу.
        force: Не использовать сохранённый текст.

    Returns:
        Optional[dict]: ``{"id", "text", "reader", "sha256"}`` или ``None``.
    """
    if force or not dedup_enabled():
        return None
    return await safe_call_async(_find_cached_text, file_path, default=None)


async def _on_document_text(scope: Scope, doc_code: Optional[str], filename: str, url: Optional[str],
                            file_path: str, text: str, force: bool = False) -> int:
    """Реализация :func:`on_document_text` (без защиты)."""
    pages = split_pages(text)
    sha = documents.file_sha256(file_path)
    eis_patch = _eis_notice_patch(file_path, filename)
    async with _db() as conn:
        ai_id = await documents.save_text(conn, sha, file_path, filename, text, documents.READER_FILE,
                                          len(pages), overwrite=force)
        doc_id = await repo.upsert_document(conn, scope, doc_code, filename, url, sha, ai_id,
                                            len(pages), Config.PE_TEXT_RETENTION_DAYS)
        if eis_patch:
            await repo.merge_document_extraction(conn, doc_id, {"eis_notice": eis_patch})
        try:
            # номер извещения из имени и шапки документа — для сопоставления с РМ, если в ссылке ЕИС его нет
            await rm_link.save_found_number(conn, scope, filename, text)
        except Exception as e:  # noqa: BLE001 — не мешает записи документа (например, до миграции 007)
            logger.warning(f"knowledge_store: номер закупки из {filename} не сохранён: {type(e).__name__}: {e}")
        return doc_id


def _eis_notice_patch(file_path: str, filename: str) -> Optional[dict]:
    """Разбирает XML извещения ЕИС и возвращает результат правил раздела 1.

    Работает только для ``.xml`` с корнем ``epNotification*``; для остальных файлов (и при любой
    ошибке разбора) возвращает ``None`` — загрузка документа от этого не зависит.

    Args:
        file_path: Локальный путь к файлу.
        filename: Исходное имя файла.

    Returns:
        Optional[dict]: ``{"version", "criteria"}`` или ``None``.
    """
    if not (filename or file_path or "").lower().endswith(".xml"):
        return None
    try:
        with open(file_path, "rb") as fh:
            notice = eis_notice.Notice.from_xml(fh.read())
        return eis_notice.findings_to_json(notice, eis_notice.evaluate_all(notice))
    except Exception:  # не извещение или битый XML — просто не извлекаем структурные факты
        return None


async def on_document_text(scope: Scope, doc_code: Optional[str], filename: str, url: Optional[str],
                           file_path: str, text: str, force: bool = False) -> Optional[int]:
    """Сохраняет текст в общий документ (по SHA-256 файла) и ссылку экспертизы на него.

    Если у документа уже есть годный текст (его прочитал риск-мониторинг или прошлый прогон), он не
    заменяется; при ``force`` текст, прочитанный тем же способом, перезаписывается.

    Args:
        scope: Экспертиза в окружении.
        doc_code: Код типа документа.
        filename: Имя файла.
        url: Исходная ссылка.
        file_path: Локальный путь к файлу (для контрольной суммы).
        text: Текст из ``FileReader.read_file`` (или повторно использованный текст общего документа).
        force: Принудительное перечитывание.

    Returns:
        Optional[int]: ``pe_documents.id`` или ``None`` (флаг выключен / ошибка).
    """
    if scope is None:
        return None
    return await safe_call_async(_on_document_text, scope, doc_code, filename, url, file_path, text, force,
                                 default=None)


def extraction_cache_enabled() -> bool:
    """Включён ли кэш результата ``TypeDataExtractor`` (``DOC_EXTRACTION_CACHE_ENABLED``, нужен и
    ``KNOWLEDGE_STORE_ENABLED``; флаги — ``configs/settings.py``).

    Returns:
        bool: ``True``, если кэш можно читать и писать.
    """
    from configs import settings
    return is_enabled() and bool(settings.DOC_EXTRACTION_CACHE_ENABLED)


async def _cached_extraction(link_id: int, version: str, model: str) -> Optional[dict]:
    """Реализация :func:`cached_extraction` (без защиты)."""
    async with _db() as conn:
        ai_id = await documents.ai_document_of_link(conn, link_id)
        return await documents.get_extraction(conn, ai_id, version, model) if ai_id else None


async def cached_extraction(link_id: Optional[int], version: str, model: str, force: bool = False) -> Optional[dict]:
    """Готовый результат ``TypeDataExtractor`` для документа ссылки или ``None`` (тогда вызывается модель).

    Args:
        link_id: ``pe_documents.id`` из :func:`on_document_text` (``None`` — хранилище выключено).
        version: :func:`knowledge_store.documents.extraction_version`.
        model: Модель LLM.
        force: Не использовать кэш.

    Returns:
        Optional[dict]: Результат извлечения.
    """
    if link_id is None or force or not extraction_cache_enabled():
        return None
    return await safe_call_async(_cached_extraction, link_id, version, model, default=None)


async def _save_extraction(link_id: int, version: str, model: str, extracted: dict) -> None:
    """Реализация :func:`save_extraction` (без защиты)."""
    async with _db() as conn:
        ai_id = await documents.ai_document_of_link(conn, link_id)
        if ai_id:
            await documents.save_extraction(conn, ai_id, version, model, extracted)


async def save_extraction(link_id: Optional[int], version: str, model: str, extracted: Any) -> None:
    """Сохраняет успешный результат ``TypeDataExtractor`` в кэш общего документа (запасные ответы не кэшируются).

    Args:
        link_id: ``pe_documents.id``.
        version: :func:`knowledge_store.documents.extraction_version`.
        model: Модель LLM.
        extracted: Результат извлечения.
    """
    if link_id is None or not extraction_cache_enabled() or not documents.cacheable_extraction(extracted):
        return
    await safe_call_async(_save_extraction, link_id, version, model, extracted)


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


async def _on_identifiers(scope: Scope, eis_number: Any) -> None:
    """Реализация :func:`on_identifiers` (без защиты)."""
    async with _db() as conn:
        await rm_link.save_identifiers(conn, scope, eis_number=eis_number)


async def on_identifiers(scope: Optional[Scope], eis_number: Any = None) -> None:
    """Запоминает реестровый номер закупки из ссылки ЕИС (для сопоставления с риск-мониторингом).

    Args:
        scope: Экспертиза в окружении (``None`` — ничего не делать).
        eis_number: Номер, который вернул разбор ссылки ЕИС.
    """
    if scope is not None and eis_number:
        await safe_call_async(_on_identifiers, scope, eis_number)


def _enqueue_indexing(scope: Scope) -> None:
    """Ставит в очередь Celery задачу индексации (ленивый импорт, чтобы не создавать циклов)."""
    from celery_app import celery_app
    celery_app.send_task("index_documents_task", args=[scope.expertise_id, scope.env], queue="evaluation")


async def _on_run_finished(scope: Scope) -> None:
    """Реализация :func:`on_run_finished` (без защиты)."""
    _enqueue_indexing(scope)


async def on_run_finished(scope: Scope) -> None:
    """Конец прогона: запускает фоновую задачу Celery: индексация (чанки + эмбеддинги), затем построение фактов.

    Args:
        scope: Экспертиза в окружении.
    """
    if scope is not None:
        await safe_call_async(_on_run_finished, scope)

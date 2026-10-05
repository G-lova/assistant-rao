"""Точки вставки для /evaluate-documents: тонкие безопасные обёртки над репозиторием.

Каждая функция сама проверяет флаг ``KNOWLEDGE_STORE_ENABLED`` и никогда не бросает исключений,
поэтому в пайплайне достаточно ``await``-вызова без try/except.
"""
import asyncio
from typing import Any, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import eis_notice, repository as repo
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
    eis_patch = _eis_notice_patch(file_path, filename)
    async with _db() as conn:
        doc_id = await repo.upsert_document(conn, expertise_id, doc_code, filename, url, sha, text,
                                            len(pages), Config.PE_TEXT_RETENTION_DAYS)
        if eis_patch:
            await repo.merge_document_extraction(conn, doc_id, {"eis_notice": eis_patch})
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


def facts_enabled() -> bool:
    """Включён ли этап фактов внутри ``/evaluate-documents``.

    Returns:
        bool: ``True``, если включены и хранилище знаний, и ``PE_FACTS_ENABLED``.
    """
    import os
    flag = os.getenv("PE_FACTS_ENABLED", str(Config.PE_FACTS_ENABLED)).lower() == "true"
    return flag and is_enabled()


async def _build_facts(expertise_id: int, http_manager: Any, llm_client: Any, llm_model: str) -> Optional[dict]:
    """Реализация :func:`build_facts` (без защиты): индексация → определение формы → факты → дайджест."""
    from knowledge_store import facts, forms, indexer, package_findings

    embedder = indexer.build_embedder(http_manager)
    index_stats = await indexer.index_expertise(int(expertise_id), embedder)
    async with _db() as conn:
        passport = await repo.get_procurement(conn, expertise_id)
        if not passport:
            return None
        code = forms.get_form_code(passport["law"], passport["check_type2"], passport["object_code"],
                                   available=await repo.list_form_codes(conn))
        await repo.set_form_code(conn, expertise_id, code)
        if not code:
            logger.info(f"knowledge_store: форма заключения для экспертизы {expertise_id} не поддерживается — факты пропущены")
            return None
        extractor = facts.FactExtractor(facts.make_llm_call(llm_client, llm_model), embedder)
        run_stats = await facts.extract_facts(conn, expertise_id, code, extractor)
        rows = await repo.get_facts(conn, expertise_id)
        fields = [dict(r) for r in await repo.get_form_fields(conn, code)]
    digest = package_findings.build_digest([dict(r) for r in rows], fields, code)
    digest["run"] = {**run_stats, "index": index_stats}
    logger.info(f"knowledge_store: факты экспертизы {expertise_id}: {digest['stats']}, запуск: {digest['run']}")
    return digest


async def _build_facts_with_timeout(expertise_id: int, http_manager: Any, llm_client: Any, llm_model: str) -> Optional[dict]:
    """Запускает :func:`_build_facts` с ограничением по времени ``PE_FACTS_TIMEOUT_SEC``."""
    return await asyncio.wait_for(_build_facts(expertise_id, http_manager, llm_client, llm_model),
                                  timeout=Config.PE_FACTS_TIMEOUT_SEC)


async def build_facts(expertise_id: int, http_manager: Any, llm_client: Any, llm_model: str) -> Optional[dict]:
    """Строит факты экспертизы по сохранённым текстам и возвращает дайджест для выводов о комплекте.

    Вызывается в ``/evaluate-documents`` после обработки всех документов и до финальной проверки
    согласованности. Требует ``KNOWLEDGE_STORE_ENABLED=true`` и ``PE_FACTS_ENABLED=true``; при любой
    ошибке или превышении времени возвращает ``None``, и пайплайн работает как раньше.

    Args:
        expertise_id: ID экспертизы.
        http_manager: Общий ``HTTPClientManager`` (для сервиса эмбеддингов).
        llm_client: OpenAI-совместимый клиент LLM.
        llm_model: Название модели.

    Returns:
        Optional[dict]: Дайджест (:func:`knowledge_store.package_findings.build_digest`) или ``None``.
    """
    if not facts_enabled():
        return None
    return await safe_call_async(_build_facts_with_timeout, expertise_id, http_manager, llm_client, llm_model, default=None)


def attach_facts_to_input(data_for_final_evaluation: dict, digest: Optional[dict]) -> None:
    """Добавляет компактный дайджест фактов во вход финальной проверки согласованности.

    Args:
        data_for_final_evaluation: Словарь, который пайплайн передаёт в ``ConsistencyChecker``.
        digest: Результат :func:`build_facts` (``None`` — ничего не делать).
    """
    if digest:
        from knowledge_store import package_findings
        # ключ ставится первым: ConsistencyChecker передаёт модели только начало JSON (первый фрагмент)
        rest = dict(data_for_final_evaluation)
        data_for_final_evaluation.clear()
        data_for_final_evaluation["facts"] = package_findings.compact_for_llm(digest)
        data_for_final_evaluation.update(rest)

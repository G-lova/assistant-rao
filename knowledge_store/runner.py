"""Фоновый этап после ``/evaluate-documents``: индексация текстов, затем построение фактов.

Факты строятся сразу после индексации, потому что следом обязательно запрашивается экспертное
заключение (сводное ЭЗ собирается из фактов). Сам ``/evaluate-documents`` на этом этапе не ждёт.
"""
import asyncio
import os
from typing import Any, Dict, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import facts, forms, indexer, repository as repo
from knowledge_store.safe import is_enabled

logger = get_logger(__name__)


def _db():
    """Возвращает менеджер соединения с БД (ленивый импорт тяжёлых зависимостей)."""
    from configs.working_with_db import get_async_db_connection
    return get_async_db_connection()


def facts_enabled() -> bool:
    """Включено ли построение фактов после индексации.

    Returns:
        bool: ``True``, если включены и хранилище знаний, и ``PE_FACTS_ENABLED``.
    """
    flag = os.getenv("PE_FACTS_ENABLED", str(Config.PE_FACTS_ENABLED)).lower() == "true"
    return flag and is_enabled()


async def build_facts(expertise_id: int, embedder: Any, llm_client: Any, llm_model: str) -> Dict[str, Any]:
    """Определяет форму заключения по паспорту закупки и строит факты (XML ЕИС + RAG/LLM).

    Повторный запуск идемпотентен: факты экспертизы заменяются целиком.

    Args:
        expertise_id: ID экспертизы.
        embedder: Объект с ``async get_embeddings(list[str])``.
        llm_client: OpenAI-совместимый клиент LLM.
        llm_model: Название модели.

    Returns:
        dict: ``{"form": код формы | None, "stats": статистика extract_facts}`` либо
        ``{"skipped": причина}``, если паспорта нет или форма не поддерживается.
    """
    async with _db() as conn:
        passport = await repo.get_procurement(conn, expertise_id)
        if not passport:
            return {"skipped": "нет паспорта закупки"}
        code = forms.get_form_code(passport["law"], passport["check_type2"], passport["object_code"],
                                   available=await repo.list_form_codes(conn))
        await repo.set_form_code(conn, expertise_id, code)
        if not code:
            logger.info(f"knowledge_store: форма заключения для экспертизы {expertise_id} не поддерживается — факты пропущены")
            return {"skipped": "форма не поддерживается"}
        extractor = facts.FactExtractor(facts.make_llm_call(llm_client, llm_model), embedder)
        stats = await facts.extract_facts(conn, expertise_id, code, extractor)
    logger.info(f"knowledge_store: факты экспертизы {expertise_id} ({code}): {stats}")
    return {"form": code, "stats": stats}


async def index_and_build_facts(expertise_id: int, http_manager: Any, llm_client: Optional[Any] = None,
                                llm_model: Optional[str] = None) -> Dict[str, Any]:
    """Индексирует документы экспертизы и, если включено, сразу строит факты.

    Ошибка или таймаут построения фактов не отменяет результат индексации: они попадают
    в поле ``facts`` результата (``{"error": ...}``).

    Args:
        expertise_id: ID экспертизы.
        http_manager: Общий ``HTTPClientManager`` (для сервиса эмбеддингов).
        llm_client: Клиент LLM (нужен только для фактов).
        llm_model: Модель LLM (нужна только для фактов).

    Returns:
        dict: ``{"index": статистика индексации, "facts": результат build_facts | {"skipped"|"error"}}``.
    """
    embedder = indexer.build_embedder(http_manager)
    result: Dict[str, Any] = {"index": await indexer.index_expertise(int(expertise_id), embedder)}
    if not facts_enabled():
        result["facts"] = {"skipped": "PE_FACTS_ENABLED=false"}
        return result
    try:
        result["facts"] = await asyncio.wait_for(
            build_facts(int(expertise_id), embedder, llm_client, llm_model), timeout=Config.PE_FACTS_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001 — сбой фактов не должен ронять задачу индексации
        logger.error(f"knowledge_store: построение фактов экспертизы {expertise_id} не удалось: {type(e).__name__}: {e}")
        result["facts"] = {"error": type(e).__name__}
    return result

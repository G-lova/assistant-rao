"""Фоновый этап после ``/evaluate-documents``: индексация текстов, затем построение фактов.

Факты строятся сразу после индексации, потому что следом обязательно запрашивается экспертное
заключение (сводное ЭЗ собирается из фактов). Сам ``/evaluate-documents`` на этом этапе не ждёт.
"""
import asyncio
import os
from typing import Any, Dict, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import facts, forms, indexer, repository as repo, rm_link
from knowledge_store.safe import is_enabled
from knowledge_store.scope import Scope

logger = get_logger(__name__)


def _db():
    """Возвращает менеджер соединения с БД: из пула задачи (:func:`knowledge_store.db.task_pool`) или временный."""
    from knowledge_store import db
    return db.connection()


def rm_link_enabled() -> bool:
    """Включено ли сопоставление экспертизы с карточкой РМ (``PE_RM_LINK_ENABLED``, нужен и хранилище знаний).

    Returns:
        bool: ``True`` — после индексации выполняется :func:`knowledge_store.rm_link.match_card`.
    """
    flag = os.getenv("PE_RM_LINK_ENABLED", str(Config.PE_RM_LINK_ENABLED)).lower() == "true"
    return flag and is_enabled()


async def link_rm(scope: Scope, http_manager: Any) -> Dict[str, Any]:
    """Сопоставляет экспертизу с карточкой закупки РМ (запросы к внешней БД окружения).

    Args:
        scope: Экспертиза в окружении.
        http_manager: Общий ``HTTPClientManager``.

    Returns:
        dict: Результат :func:`knowledge_store.rm_link.match_card`.
    """
    async with _db() as conn:
        return await rm_link.match_card(conn, scope, rm_link.external_fetch(http_manager, scope.env))


def facts_enabled() -> bool:
    """Включено ли построение фактов после индексации.

    Returns:
        bool: ``True``, если включены и хранилище знаний, и ``PE_FACTS_ENABLED``.
    """
    flag = os.getenv("PE_FACTS_ENABLED", str(Config.PE_FACTS_ENABLED)).lower() == "true"
    return flag and is_enabled()


async def build_facts(scope: Scope, embedder: Any, llm_client: Any, llm_model: str) -> Dict[str, Any]:
    """Определяет форму заключения по паспорту закупки и строит факты (XML ЕИС + RAG/LLM).

    Повторный запуск идемпотентен: факты экспертизы заменяются целиком.

    Args:
        scope: Экспертиза в окружении.
        embedder: Объект с ``async get_embeddings(list[str])``.
        llm_client: OpenAI-совместимый клиент LLM.
        llm_model: Название модели.

    Returns:
        dict: ``{"form": код формы | None, "stats": статистика extract_facts}`` либо
        ``{"skipped": причина}``, если паспорта нет или форма не поддерживается.
    """
    async with _db() as conn:
        passport = await repo.get_procurement(conn, scope)
        if not passport:
            return {"skipped": "нет паспорта закупки"}
        code = forms.get_form_code(passport["law"], passport["check_type2"], passport["object_code"],
                                   available=await repo.list_form_codes(conn))
        await repo.set_form_code(conn, scope, code)
        if not code:
            logger.info(f"knowledge_store: форма заключения для экспертизы {scope} не поддерживается — факты пропущены")
            return {"skipped": "форма не поддерживается"}
        extractor = facts.FactExtractor(facts.make_llm_call(llm_client, llm_model), embedder)
        stats = await facts.extract_facts(conn, scope, code, extractor)
    logger.info(f"knowledge_store: факты экспертизы {scope} ({code}): {stats}")
    return {"form": code, "stats": stats}


async def index_and_build_facts(scope: Scope, http_manager: Any, llm_client: Optional[Any] = None,
                                llm_model: Optional[str] = None) -> Dict[str, Any]:
    """Индексирует документы экспертизы и, если включено, сразу строит факты.

    Ошибка или таймаут построения фактов не отменяет результат индексации: они попадают
    в поле ``facts`` результата (``{"error": ...}``).

    Args:
        scope: Экспертиза в окружении.
        http_manager: Общий ``HTTPClientManager`` (для сервиса эмбеддингов).
        llm_client: Клиент LLM (нужен только для фактов).
        llm_model: Модель LLM (нужна только для фактов).

    Returns:
        dict: ``{"index": статистика индексации, "rm_link": сопоставление с РМ (если включено),
        "facts": результат build_facts | {"skipped"|"error"}}``.
    """
    embedder = indexer.build_embedder(http_manager)
    result: Dict[str, Any] = {"index": await indexer.index_expertise(scope, embedder)}
    if rm_link_enabled():
        try:
            result["rm_link"] = await link_rm(scope, http_manager)
        except Exception as e:  # noqa: BLE001 — сопоставление с РМ не должно мешать фактам
            logger.error(f"knowledge_store: сопоставление {scope} с РМ не удалось: {type(e).__name__}: {e}")
            result["rm_link"] = {"error": type(e).__name__}
    if not facts_enabled():
        result["facts"] = {"skipped": "PE_FACTS_ENABLED=false"}
        return result
    try:
        result["facts"] = await asyncio.wait_for(
            build_facts(scope, embedder, llm_client, llm_model), timeout=Config.PE_FACTS_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001 — сбой фактов не должен ронять задачу индексации
        logger.error(f"knowledge_store: построение фактов экспертизы {scope} не удалось: {type(e).__name__}: {e}")
        result["facts"] = {"error": type(e).__name__}
    return result

"""Запуск синхронизации и обработки публикаций экспертов (Celery-задачи ``tasks.expert_publications_*``)."""
from typing import Any, Dict, Optional, Sequence

from configs import settings
from configs.logger import get_logger

logger = get_logger(__name__)


async def run(env: str, expert_ids: Optional[Sequence[int]] = None, sync: bool = True, process: bool = True) -> Dict[str, Any]:
    """Синхронизирует источники с внешней БД и обрабатывает пачку очереди.

    Args:
        env: Окружение внешней БД (``X-API-Database``).
        expert_ids: Только эти эксперты (вызов после сохранения анкеты); ``None`` — полная синхронизация.
        sync: Сравнить снимок внешней БД с внутренней.
        process: Обработать пачку очереди.

    Returns:
        Dict[str, Any]: ``env``, ``sync`` (счётчики сравнения), ``process`` (счётчики обработки), ``stats``,
        ``more`` — в очереди ещё есть источники (задача поставит продолжение).
    """
    from configs.config import Config
    from configs.data_fetcher import DataFetcher
    from configs.http_client_manager import HTTPClientManager
    from knowledge_store.db import connection, task_pool
    from knowledge_store.scope import normalize_env
    from search_experts.publications import repo
    from search_experts.publications.processor import PublicationProcessor
    from search_experts.publications.sync import apply_snapshot, fetch_snapshot

    env = normalize_env(env)
    result: Dict[str, Any] = {"env": env}
    async with HTTPClientManager(timeout=120.0) as http, task_pool():
        if sync:
            db_config = Config.get_database_config(env)
            fetcher = DataFetcher(db_config.url, db_config.headers, http)
            snapshot = await fetch_snapshot(fetcher, db_config, expert_ids)
            async with connection() as conn:
                result["sync"] = await apply_snapshot(conn, env, snapshot, full=not expert_ids,
                                                      link_types=tuple(settings.EXPERT_PUB_LINK_TYPES))
            logger.info(f"Публикации экспертов {env}: синхронизация {result['sync']}")
        if process:
            processor = PublicationProcessor(env, http)
            result["process"] = await processor.process_batch()
            # публикации, оставшиеся без вектора (сбой сервиса эмбеддингов, смена модели)
            result["process"]["embedded"] = await processor.embed_publications(limit=500)
            logger.info(f"Публикации экспертов {env}: обработка {result['process']}")
        async with connection() as conn:
            result["stats"] = await repo.stats(conn, env)
    proc = result.get("process") or {}
    result["more"] = bool(proc.get("released")) or (proc.get("claimed", 0) >= settings.EXPERT_PUB_BATCH)
    return result

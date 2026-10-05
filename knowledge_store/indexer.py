"""Индексация документов экспертизы: страницы -> чанки -> эмбеддинги -> ``pe_chunks``."""
from typing import Any, Dict, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import repository as repo
from knowledge_store.pages import chunk_pages, split_pages

logger = get_logger(__name__)


def build_embedder(http_manager):
    """Создаёт клиент эмбеддингов из конфигурации приложения.

    Args:
        http_manager: Общий ``HTTPClientManager``.

    Returns:
        EmbeddingClient: Клиент сервиса эмбеддингов.
    """
    from search_experts.embedding_client import EmbeddingClient
    cfg = Config.get_embedding_config()
    return EmbeddingClient(cfg.api_url, cfg.api_key, cfg.model, batch_size=cfg.batch_size, http_manager=http_manager)


def _db():
    """Возвращает менеджер соединения с БД (ленивый импорт тяжёлых зависимостей)."""
    from configs.working_with_db import get_async_db_connection
    return get_async_db_connection()


async def index_expertise(expertise_id: int, embedder: Any, max_chars: int = 1500, overlap: int = 150) -> Dict[str, int]:
    """Строит чанки и эмбеддинги для всех ещё не проиндексированных документов экспертизы.

    Документ индексируется «всё или ничего»: если сервис эмбеддингов вернул не все векторы
    (``EmbeddingClient`` молча пропускает упавшие пакеты), документ не сохраняется и будет
    повторно обработан при следующем запуске.

    Args:
        expertise_id: ID экспертизы.
        embedder: Объект с ``async get_embeddings(list[str])``.
        max_chars: Размер чанка в символах.
        overlap: Перекрытие чанков в символах.

    Returns:
        dict: Статистика ``{"documents": N, "chunks": M, "failed": K}``.
    """
    stats = {"documents": 0, "chunks": 0, "failed": 0}
    async with _db() as conn:
        docs = await repo.documents_for_indexing(conn, expertise_id)
        for doc in docs:
            try:
                chunks = chunk_pages(split_pages(doc["text_full"]), max_chars=max_chars, overlap=overlap)
                if not chunks:
                    continue
                vectors = await embedder.get_embeddings([c.text for c in chunks])
                if len(vectors) != len(chunks):
                    raise RuntimeError(f"получено {len(vectors)} эмбеддингов из {len(chunks)}")
                async with conn.transaction():
                    stats["chunks"] += await repo.replace_chunks(conn, doc["id"], expertise_id, chunks, [list(v) for v in vectors])
                stats["documents"] += 1
            except Exception as e:  # noqa: BLE001 — один плохой документ не должен останавливать остальные
                stats["failed"] += 1
                logger.error(f"knowledge_store: индексация документа {doc['id']} не удалась: {type(e).__name__}: {e}")
    logger.info(f"knowledge_store: индексация экспертизы {expertise_id}: {stats}")
    return stats


async def purge_expired_texts() -> int:
    """Очищает тексты и чанки с истёкшим сроком хранения.

    Returns:
        int: Число очищенных документов.
    """
    async with _db() as conn:
        n = await repo.purge_expired(conn)
    logger.info(f"knowledge_store: очищено документов по сроку хранения: {n}")
    return n

"""Индексация документов экспертизы: страницы -> чанки (нарезка ``PE_CHUNKER``) -> эмбеддинги -> ``ai_document_chunks``.

Чанки принадлежат общему документу (``ai_documents``): документ, уже проиндексированный для другой
экспертизы, повторно не индексируется.
"""
import os
from typing import Any, Dict, Optional

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import repository as repo
from knowledge_store.pages import chunk_document, current_chunker, embedding_text
from knowledge_store.scope import Scope

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
    """Возвращает менеджер соединения с БД: из пула задачи (:func:`knowledge_store.db.task_pool`) или временный."""
    from knowledge_store import db
    return db.connection()


def embedding_model_name() -> str:
    """Имя модели эмбеддингов — часть ключа чанков (векторы разных моделей несравнимы).

    Returns:
        str: ``EMBEDDING_MODEL`` из конфигурации (``default``, если не задана).
    """
    return os.getenv("EMBEDDING_MODEL") or Config.EMBEDDING_MODEL or "default"


async def index_expertise(scope: Scope, embedder: Any) -> Dict[str, int]:
    """Строит чанки и эмбеддинги для всех ещё не проиндексированных документов экспертизы.

    Документ индексируется «всё или ничего»: если сервис эмбеддингов вернул не все векторы
    (``EmbeddingClient`` молча пропускает упавшие пакеты), документ не сохраняется и будет
    повторно обработан при следующем запуске.

    Args:
        scope: Экспертиза в окружении.
        embedder: Объект с ``async get_embeddings(list[str])``.

    Returns:
        dict: Статистика ``{"documents": N, "chunks": M, "failed": K}``.
    """
    stats = {"documents": 0, "chunks": 0, "failed": 0}
    async with _db() as conn:
        model = embedding_model_name()
        chunker = current_chunker()
        docs = await repo.documents_for_indexing(conn, scope, chunker, model)
        for doc in docs:
            try:
                chunks = chunk_document(doc["text_full"], chunker)
                if not chunks:
                    continue
                vectors = await embedder.get_embeddings([embedding_text(c) for c in chunks])
                if len(vectors) != len(chunks):
                    raise RuntimeError(f"получено {len(vectors)} эмбеддингов из {len(chunks)}")
                async with conn.transaction():
                    stats["chunks"] += await repo.replace_chunks(conn, doc["id"], chunker, model, chunks,
                                                                 [list(v) for v in vectors])
                stats["documents"] += 1
            except Exception as e:  # noqa: BLE001 — один плохой документ не должен останавливать остальные
                stats["failed"] += 1
                logger.error(f"knowledge_store: индексация документа {doc['id']} не удалась: {type(e).__name__}: {e}")
    logger.info(f"knowledge_store: индексация экспертизы {scope}: {stats}")
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

"""Кэш эмбеддингов текстов во внутренней БД (``text_embeddings``, миграция 013).

Подбор экспертов на каждый запрос считал эмбеддинги текстов всех кандидатов: 369 текстов — 17 с из 26 с
(экспертиза 7228, stage, 05.11.2026; сервис llmhub, модель bge-m3). Тексты анкет меняются редко, поэтому вектор
хранится по SHA-256 текста и модели, а в сервис уходят только новые и изменённые тексты.

Если внутренняя БД недоступна, векторы считаются сервисом как раньше (подбор не останавливается).
"""
import hashlib
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from configs.logger import get_logger

logger = get_logger(__name__)

# last_used_at обновляется не чаще раза в сутки — иначе каждый подбор писал бы сотни строк
TOUCH_AFTER = "1 day"


def text_sha(text: str) -> str:
    """SHA-256 текста (ключ кэша вместе с моделью).

    Args:
        text: Текст.

    Returns:
        str: 64 шестнадцатеричных символа.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def load(conn, model: str, shas: Sequence[str]) -> Dict[str, List[float]]:
    """Векторы из кэша; отметка использования у давно не использованных.

    Args:
        conn: Соединение с внутренней БД.
        model: Модель эмбеддингов.
        shas: Хэши текстов.

    Returns:
        Dict[str, List[float]]: Хэш → вектор (только найденные).
    """
    if not shas:
        return {}
    rows = await conn.fetch("SELECT text_sha, embedding FROM text_embeddings WHERE model = $1 AND text_sha = ANY($2::text[])",
                            model, list(shas))
    found = {r["text_sha"].strip(): list(r["embedding"]) for r in rows}
    if found:
        await conn.execute(
            f"UPDATE text_embeddings SET last_used_at = NOW() WHERE model = $1 AND text_sha = ANY($2::text[]) "
            f"AND last_used_at < NOW() - interval '{TOUCH_AFTER}'", model, list(found))
    return found


async def save(conn, model: str, vectors: Dict[str, Sequence[float]]) -> None:
    """Записывает новые векторы (существующие не трогаются).

    Args:
        conn: Соединение с внутренней БД.
        model: Модель эмбеддингов.
        vectors: Хэш → вектор.
    """
    params = [(model, sha, [float(x) for x in vec]) for sha, vec in vectors.items()]
    if params:
        await conn.executemany(
            "INSERT INTO text_embeddings (model, text_sha, embedding) VALUES ($1, $2, $3::real[]) "
            "ON CONFLICT (model, text_sha) DO NOTHING", params)


async def purge_unused(conn, days: int = 90) -> int:
    """Удаляет векторы, не использованные ``days`` дней.

    Args:
        conn: Соединение с внутренней БД.
        days: Срок хранения без использования.

    Returns:
        int: Сколько удалено.
    """
    rows = await conn.fetch(
        "DELETE FROM text_embeddings WHERE last_used_at < NOW() - make_interval(days => $1) RETURNING text_sha", int(days))
    return len(rows)


async def get_embeddings(client, texts: Sequence[str], model: Optional[str] = None,
                         connection: Optional[Callable] = None) -> np.ndarray:
    """Эмбеддинги текстов: из кэша, недостающие — сервисом (с записью в кэш).

    Повторяющиеся тексты считаются один раз. Порядок результата — как у ``texts``.

    Args:
        client: ``configs.embedding_client.EmbeddingClient``.
        texts: Тексты.
        model: Модель (по умолчанию ``client.model``).
        connection: Фабрика соединений (по умолчанию ``knowledge_store.db.connection``).

    Returns:
        numpy.ndarray: Матрица ``len(texts) × размерность``.
    """
    model = model or client.model
    shas = [text_sha(t) for t in texts]
    unique = list(dict.fromkeys(shas))
    if connection is None:
        from knowledge_store.db import connection

    cached: Dict[str, List[float]] = {}
    try:
        async with connection() as conn:
            cached = await load(conn, model, unique)
    except Exception as e:  # noqa: BLE001 — без кэша считаем сервисом
        logger.warning(f"Кэш эмбеддингов недоступен: {type(e).__name__}: {e}")
        connection = None

    missing = [s for s in unique if s not in cached]
    if missing:
        first_text = {}
        for s, t in zip(shas, texts):
            first_text.setdefault(s, t)
        vectors = await client.get_embeddings([first_text[s] for s in missing], strict=True)
        fresh = {s: list(map(float, v)) for s, v in zip(missing, vectors)}
        cached.update(fresh)
        if connection is not None:
            try:
                async with connection() as conn:
                    await save(conn, model, fresh)
            except Exception as e:  # noqa: BLE001 — векторы уже есть, кэш дополнится в следующий раз
                logger.warning(f"Эмбеддинги не записаны в кэш: {type(e).__name__}: {e}")
    logger.info(f"Эмбеддинги: {len(unique)} текстов, из кэша {len(unique) - len(missing)}, посчитано {len(missing)}")
    return np.array([cached[s] for s in shas], dtype=np.float32)

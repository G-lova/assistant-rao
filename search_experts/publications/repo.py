"""SQL таблиц публикаций экспертов во внутренней БД (миграция 011).

Функции принимают соединение ``asyncpg`` (или совместимое — ``tests.pg_psql.PsqlConn``) и окружение ``env``:
одна внутренняя БД обслуживает все окружения внешней.
"""
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from knowledge_store.documents import strip_nul

# Статусы источника
PENDING, PROCESSING, DONE, EMPTY, FAILED, UNSUPPORTED, REMOVED = (
    "pending", "processing", "done", "empty", "failed", "unsupported", "removed")
# «Зависшая» обработка (воркер снят при перезапуске) снова берётся в работу через столько минут
STALE_PROCESSING_MIN = 90


def _vec(v: Sequence[float]) -> str:
    """Формат pgvector ``'[0.1,0.2,…]'`` (приводится к vector через ``$n::vector``)."""
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


def _load(value: Any) -> Any:
    """JSONB из драйвера: asyncpg без кодека возвращает строку."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


# ---------------------------------------------------------------- эксперты

async def load_experts(conn, env: str, expert_ids: Optional[Sequence[int]] = None) -> Dict[int, dict]:
    """Сохранённое состояние экспертов: имя, отпечаток источников, признак удаления.

    Args:
        conn: Соединение.
        env: Окружение.
        expert_ids: Только эти эксперты (``None`` — все).

    Returns:
        Dict[int, dict]: ``expert_id`` → ``{"name", "sources_hash", "deleted_at"}``.
    """
    if expert_ids is None:
        rows = await conn.fetch(
            "SELECT expert_id, name, sources_hash, deleted_at FROM expert_pub_experts WHERE env = $1", env)
    else:
        rows = await conn.fetch(
            "SELECT expert_id, name, sources_hash, deleted_at FROM expert_pub_experts "
            "WHERE env = $1 AND expert_id = ANY($2::bigint[])", env, [int(i) for i in expert_ids])
    return {int(r["expert_id"]): dict(r) for r in rows}


async def upsert_expert(conn, env: str, expert_id: int, name: Optional[str], sources_hash: Optional[str],
                        deleted: bool = False) -> None:
    """Записывает состояние эксперта после синхронизации.

    Args:
        conn: Соединение.
        env: Окружение.
        expert_id: ID эксперта.
        name: ФИО.
        sources_hash: Отпечаток источников.
        deleted: Эксперт удалён во внешней БД.
    """
    await conn.execute(
        """
        INSERT INTO expert_pub_experts (env, expert_id, name, sources_hash, deleted_at, synced_at)
        VALUES ($1, $2, $3, $4, CASE WHEN $5 THEN NOW() END, NOW())
        ON CONFLICT (env, expert_id) DO UPDATE SET
            name = EXCLUDED.name, sources_hash = EXCLUDED.sources_hash, synced_at = NOW(),
            deleted_at = CASE WHEN $5 THEN COALESCE(expert_pub_experts.deleted_at, NOW()) END
        """,
        env, int(expert_id), name, sources_hash, bool(deleted))


async def touch_experts(conn, env: str, expert_ids: Sequence[int]) -> None:
    """Отмечает время синхронизации экспертов без изменений.

    Args:
        conn: Соединение.
        env: Окружение.
        expert_ids: ID экспертов.
    """
    if expert_ids:
        await conn.execute("UPDATE expert_pub_experts SET synced_at = NOW() WHERE env = $1 AND expert_id = ANY($2::bigint[])",
                           env, [int(i) for i in expert_ids])


# ---------------------------------------------------------------- источники

SQL_UPSERT_SOURCE = """
INSERT INTO expert_pub_sources (env, expert_id, source_kind, source_key, collection, url, file_name, link_type, status)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (env, expert_id, source_kind, source_key) DO UPDATE SET
    collection   = EXCLUDED.collection,
    url          = EXCLUDED.url,
    file_name    = EXCLUDED.file_name,
    link_type    = EXCLUDED.link_type,
    last_seen_at = NOW(),
    removed_at   = NULL,
    attempts     = CASE WHEN expert_pub_sources.status = 'removed' THEN 0 ELSE expert_pub_sources.attempts END,
    status       = CASE
        -- источник вернулся в анкету (удалили и загрузили снова): обрабатывается заново (файл — из кэша по хэшу)
        WHEN expert_pub_sources.status = 'removed' THEN EXCLUDED.status
        -- тип ссылки включили в настройках
        WHEN expert_pub_sources.status = 'unsupported' AND EXCLUDED.status = 'pending' THEN 'pending'
        -- тип ссылки выключили: необработанное не обрабатывать
        WHEN EXCLUDED.status = 'unsupported' AND expert_pub_sources.status IN ('pending', 'failed') THEN 'unsupported'
        ELSE expert_pub_sources.status
    END
"""


async def upsert_sources(conn, env: str, rows: Iterable[tuple]) -> None:
    """Добавляет новые источники и обновляет существующие (``last_seen_at``, возврат удалённых).

    Args:
        conn: Соединение.
        env: Окружение.
        rows: Кортежи ``(expert_id, source_kind, source_key, collection, url, file_name, link_type, status)``.
    """
    params = [(env, *r) for r in rows]
    if params:
        await conn.executemany(SQL_UPSERT_SOURCE, params)


async def remove_missing_sources(conn, env: str, expert_id: int, keep_keys: Sequence[str]) -> int:
    """Помечает ``removed`` источники эксперта, которых больше нет в анкете.

    Публикации удалённого источника остаются в БД, но в similarity не участвуют (учитываются только ``done``).

    Args:
        conn: Соединение.
        env: Окружение.
        expert_id: ID эксперта.
        keep_keys: Текущие источники как ``"<source_kind>:<source_key>"``.

    Returns:
        int: Сколько источников помечено.
    """
    rows = await conn.fetch(
        """
        UPDATE expert_pub_sources SET status = 'removed', removed_at = NOW()
        WHERE env = $1 AND expert_id = $2 AND status <> 'removed'
          AND NOT ((source_kind || ':' || source_key) = ANY($3::text[]))
        RETURNING id
        """,
        env, int(expert_id), list(keep_keys))
    return len(rows)


async def claim_sources(conn, env: str, limit: int, retry_hours: int, max_attempts: int) -> List[dict]:
    """Берёт в работу источники очереди: новые, неудавшиеся (пора повторить) и «зависшие».

    Статус меняется на ``processing`` одним запросом — параллельный запуск задачи те же источники не возьмёт.

    Args:
        conn: Соединение.
        env: Окружение.
        limit: Сколько взять.
        retry_hours: Неудавшийся источник повторяется не раньше, чем через столько часов.
        max_attempts: Максимум попыток.

    Returns:
        List[dict]: Источники с полями таблицы и ФИО эксперта (``expert_name``).
    """
    rows = await conn.fetch(
        f"""
        UPDATE expert_pub_sources s SET status = 'processing', processed_at = NOW(), attempts = s.attempts + 1
        FROM (
            SELECT id FROM expert_pub_sources
            WHERE env = $1 AND (
                status = 'pending'
                OR (status = 'failed' AND attempts < $3 AND processed_at < NOW() - make_interval(hours => $2))
                OR (status = 'processing' AND processed_at < NOW() - interval '{STALE_PROCESSING_MIN} minutes'))
            ORDER BY (status = 'failed'), first_seen_at, id
            LIMIT $4
            FOR UPDATE SKIP LOCKED
        ) q
        WHERE s.id = q.id
        RETURNING s.id, s.env, s.expert_id, s.source_kind, s.source_key, s.collection, s.url, s.file_name, s.link_type,
                  s.attempts, (SELECT e.name FROM expert_pub_experts e WHERE e.env = s.env AND e.expert_id = s.expert_id) AS expert_name
        """,
        env, int(retry_hours), int(max_attempts), int(limit))
    return [dict(r) for r in rows]


async def release_sources(conn, source_ids: Sequence[int]) -> None:
    """Возвращает в очередь взятые, но не обработанные источники (кончилось время запуска).

    Args:
        conn: Соединение.
        source_ids: ID источников.
    """
    if source_ids:
        await conn.execute(
            "UPDATE expert_pub_sources SET status = 'pending', attempts = GREATEST(attempts - 1, 0) "
            "WHERE id = ANY($1::bigint[]) AND status = 'processing'", [int(i) for i in source_ids])


async def fail_source(conn, source_id: int, error: str) -> None:
    """Отмечает неудачу обработки источника.

    Args:
        conn: Соединение.
        source_id: ID источника.
        error: Причина (обрезается до 1000 символов).
    """
    await conn.execute(
        "UPDATE expert_pub_sources SET status = 'failed', error = $2, processed_at = NOW() WHERE id = $1 AND status = 'processing'",
        int(source_id), strip_nul(str(error))[:1000])


async def save_source_result(conn, source: dict, publications: List[dict], document_id: Optional[int]) -> List[int]:
    """Заменяет публикации источника результатом разбора и отмечает источник обработанным.

    Если за время обработки источник удалили из анкеты (``removed``), статус не меняется.

    Args:
        conn: Соединение.
        source: Источник (``id``, ``env``, ``expert_id``).
        publications: Записи публикаций (см. :func:`search_experts.publications.extract.normalize_publication`)
            с ``author_verified``.
        document_id: ``ai_documents.id`` для файла.

    Returns:
        List[int]: ID записанных публикаций.
    """
    status = DONE if publications else EMPTY
    async with conn.transaction():
        await conn.execute("DELETE FROM expert_publications WHERE source_id = $1", int(source["id"]))
        ids = []
        for no, p in enumerate(publications):
            pid = await conn.fetchval(
                """
                INSERT INTO expert_publications
                    (source_id, env, expert_id, pub_no, title, year, venue, pub_type, authors, keywords, abstract, doi,
                     udk, author_verified)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14)
                RETURNING id
                """,
                int(source["id"]), source["env"], int(source["expert_id"]), no, strip_nul(p["title"]), p.get("year"),
                strip_nul(p.get("venue")), p.get("pub_type"),
                json.dumps(p["authors"], ensure_ascii=False) if p.get("authors") is not None else None,
                strip_nul(p.get("keywords")), strip_nul(p.get("abstract")), p.get("doi"), p.get("udk"),
                p.get("author_verified"))
            ids.append(int(pid))
        await conn.execute(
            """
            UPDATE expert_pub_sources SET status = CASE WHEN status = 'removed' THEN status ELSE $2 END,
                   document_id = $3, error = NULL, processed_at = NOW()
            WHERE id = $1
            """,
            int(source["id"]), status, document_id)
    return ids


# ---------------------------------------------------------------- публикации и эмбеддинги

async def publications_without_embedding(conn, env: str, embedding_model: str, limit: int,
                                         source_ids: Optional[Sequence[int]] = None) -> List[dict]:
    """Публикации без эмбеддинга текущей модели.

    Args:
        conn: Соединение.
        env: Окружение.
        embedding_model: Модель эмбеддингов.
        limit: Максимум строк.
        source_ids: Только публикации этих источников.

    Returns:
        List[dict]: ``id``, ``title``, ``keywords``, ``abstract``, ``venue``, ``embed_text_sha``.
    """
    flt = "AND source_id = ANY($4::bigint[])" if source_ids is not None else ""
    params = [env, embedding_model, int(limit)] + ([[int(i) for i in source_ids]] if source_ids is not None else [])
    rows = await conn.fetch(
        f"""
        SELECT id, title, keywords, abstract, venue, embed_text_sha FROM expert_publications
        WHERE env = $1 AND (embedding IS NULL OR embedding_model IS DISTINCT FROM $2) {flt}
        ORDER BY id LIMIT $3
        """, *params)
    return [dict(r) for r in rows]


async def save_embeddings(conn, rows: Iterable[tuple], embedding_model: str) -> None:
    """Записывает эмбеддинги публикаций.

    Args:
        conn: Соединение.
        rows: Кортежи ``(publication_id, embed_text_sha, vector)``.
        embedding_model: Модель эмбеддингов.
    """
    params = [(int(pid), sha, _vec(vec), embedding_model) for pid, sha, vec in rows]
    if params:
        await conn.executemany(
            "UPDATE expert_publications SET embed_text_sha = $2, embedding = $3::vector, embedding_model = $4 WHERE id = $1",
            params)


async def publications_of_expert(conn, env: str, expert_id: int) -> List[dict]:
    """Публикации эксперта (все источники) — для повторной проверки авторства после смены ФИО.

    Args:
        conn: Соединение.
        env: Окружение.
        expert_id: ID эксперта.

    Returns:
        List[dict]: ``id``, ``authors``, ``author_verified``.
    """
    rows = await conn.fetch("SELECT id, authors, author_verified FROM expert_publications WHERE env = $1 AND expert_id = $2",
                            env, int(expert_id))
    return [{**dict(r), "authors": _load(r["authors"])} for r in rows]


async def set_author_verified(conn, rows: Iterable[tuple]) -> None:
    """Обновляет признак авторства.

    Args:
        conn: Соединение.
        rows: Кортежи ``(publication_id, author_verified)``.
    """
    params = [(int(pid), v) for pid, v in rows]
    if params:
        await conn.executemany("UPDATE expert_publications SET author_verified = $2 WHERE id = $1", params)


async def publication_similarities(conn, env: str, expert_ids: Sequence[int], embedding: Sequence[float],
                                   embedding_model: str, require_author: bool) -> List[dict]:
    """Сходство каждой учитываемой публикации экспертов с эмбеддингом экспертизы.

    Учитываются публикации источников ``done`` (удалённые из анкеты — нет) с эмбеддингом текущей модели; при
    ``require_author`` — кроме тех, где эксперта нет среди авторов (``author_verified = false``).

    Args:
        conn: Соединение.
        env: Окружение.
        expert_ids: Кандидаты.
        embedding: Эмбеддинг текста экспертизы.
        embedding_model: Модель эмбеддингов.
        require_author: Не учитывать публикации с ``author_verified = false``.

    Returns:
        List[dict]: ``expert_id``, ``publication_id``, ``title``, ``year``, ``similarity``.
    """
    author = "AND p.author_verified IS NOT FALSE" if require_author else ""
    rows = await conn.fetch(
        f"""
        SELECT p.expert_id, p.id AS publication_id, p.title, p.year, 1 - (p.embedding <=> $3::vector) AS similarity
        FROM expert_publications p
        JOIN expert_pub_sources s ON s.id = p.source_id AND s.status = 'done'
        WHERE p.env = $1 AND p.expert_id = ANY($2::bigint[]) AND p.embedding_model = $4 AND p.embedding IS NOT NULL {author}
        """,
        env, [int(i) for i in expert_ids], _vec(embedding), embedding_model)
    return [dict(r) for r in rows]


async def stats(conn, env: str) -> Dict[str, Any]:
    """Сводка по источникам и публикациям окружения (для ответа задачи и журнала).

    Args:
        conn: Соединение.
        env: Окружение.

    Returns:
        Dict[str, Any]: ``sources`` (статус → число), ``experts_with_publications``, ``publications``.
    """
    rows = await conn.fetch("SELECT status, count(*) AS n FROM expert_pub_sources WHERE env = $1 GROUP BY status", env)
    pubs = await conn.fetchrow(
        """
        SELECT count(*) AS publications, count(DISTINCT p.expert_id) AS experts
        FROM expert_publications p JOIN expert_pub_sources s ON s.id = p.source_id AND s.status = 'done'
        WHERE p.env = $1
        """, env)
    return {"sources": {r["status"]: int(r["n"]) for r in rows},
            "publications": int(pubs["publications"]) if pubs else 0,
            "experts_with_publications": int(pubs["experts"]) if pubs else 0}

import json
from typing import Any, Dict, List, Optional, Sequence

import asyncpg

from configs.config import Config
from configs.logger import get_logger
from knowledge_store.documents import strip_nul

# Схема нарезки чанков риск-мониторинга (split_large_text, 800 токенов). Чанки экспертизы того же документа
# лежат в той же таблице с chunker = 'pages_v2'. Условие пишется литералом: только так планировщик выбирает
# частичный HNSW-индекс idx_ai_chunks_emb_rm (миграция 005).
RM_CHUNKER = "rm_tokens_800"
RM_CHUNKER_SQL = f"chunker = '{RM_CHUNKER}'"

logger = get_logger(__name__)


def _load(v: Any) -> Any:
    """asyncpg без кодека возвращает jsonb строкой."""
    return json.loads(v) if isinstance(v, str) else v


def _vec(v: Sequence[float]) -> str:
    """Формат pgvector: '[0.1,0.2,...]' (приводится к vector через $n::vector)."""
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


class AIRepo:
    """
    Репозиторий таблиц ai_documents / ai_document_chunks / ai_document_analysis / ai_file_links.

    Пул создаётся внутри async-контекста (как HTTPClientManager), потому что Celery-задача
    запускает собственный event loop через asyncio.run(), и глобальный пул к нему привязать нельзя.

        async with AIRepo() as repo:
            ...
    """

    def __init__(self, min_size: int = 1, max_size: int = 4):
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Optional[asyncpg.Pool] = None

    async def __aenter__(self):
        """Создаёт пул соединений с Postgres сервиса (параметры ``DB_*``)."""
        self.pool = await asyncpg.create_pool(
            host=Config.DB_HOST,
            port=int(Config.DB_PORT),
            database=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            min_size=self.min_size,
            max_size=self.max_size,
            max_inactive_connection_lifetime=300,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Закрывает пул соединений."""
        if self.pool:
            await self.pool.close()

    # ---------- ссылки внешний файл -> документ ----------

    async def get_link(self, env: str, external_file_id: int) -> Optional[asyncpg.Record]:
        """Документ, связанный с внешним файлом ``risk_monitoring_files.id``, и хэш его содержимого.

        Args:
            env: Окружение внешней системы (dev / stage / prod) — id файлов в окружениях разные.
            external_file_id: ``risk_monitoring_files.id``.

        Returns:
            Optional[asyncpg.Record]: ``document_id``, ``content_sha256`` или ``None``.
        """
        return await self.pool.fetchrow(
            """
            SELECT l.document_id, d.content_sha256
            FROM ai_file_links l JOIN ai_documents d ON d.id = l.document_id
            WHERE l.env=$1 AND l.external_file_id=$2
            """,
            env, external_file_id,
        )

    async def link_file(self, env: str, external_file_id: int, document_id: int,
                        card_id: Optional[int] = None) -> None:
        """Связывает внешний файл с документом (повторная связь перезаписывается).

        Новая ссылка живёт не дольше ``RM_TEXT_MAX_RETENTION_DAYS`` от загрузки; при закрытии закупки срок
        сокращается (:meth:`close_card`). Повторная связь срок и закрытие не меняет.

        Args:
            env: Окружение внешней системы.
            external_file_id: ``risk_monitoring_files.id``.
            document_id: ``ai_documents.id``.
            card_id: ``risk_monitoring_contracts.id`` — карточка закупки файла (для срока хранения).
        """
        await self.pool.execute(
            """
            INSERT INTO ai_file_links (env, external_file_id, document_id, rm_card_id, expires_at)
            VALUES ($1, $2, $3, $4, NOW() + make_interval(days => $5))
            ON CONFLICT (env, external_file_id)
            DO UPDATE SET document_id = EXCLUDED.document_id, linked_at = NOW(),
                          rm_card_id = COALESCE(EXCLUDED.rm_card_id, ai_file_links.rm_card_id),
                          expires_at = COALESCE(ai_file_links.expires_at, EXCLUDED.expires_at)
            """,
            env, external_file_id, document_id, card_id, Config.RM_TEXT_MAX_RETENTION_DAYS,
        )

    async def close_card(self, env: str, card_id: int, closed_at: Any) -> int:
        """Закупка/контракт закрыты: ссылки файлов карточки живут ещё ``RM_TEXT_AFTER_CLOSE_DAYS`` после закрытия.

        Срок только сокращается (не дольше прежнего потолка); повторное закрытие не сдвигает дату.

        Args:
            env: Окружение внешней системы.
            card_id: ``risk_monitoring_contracts.id``.
            closed_at: Дата закрытия (``datetime``).

        Returns:
            int: Число ссылок, для которых зафиксировано закрытие.
        """
        rows = await self.pool.fetch(
            """
            UPDATE ai_file_links
            SET closed_at = COALESCE(closed_at, $3),
                expires_at = LEAST(COALESCE(expires_at, 'infinity'::timestamptz),
                                   COALESCE(closed_at, $3) + make_interval(days => $4))
            WHERE env = $1 AND rm_card_id = $2
            RETURNING external_file_id
            """,
            env, int(card_id), closed_at, Config.RM_TEXT_AFTER_CLOSE_DAYS,
        )
        return len(rows)

    async def reopen_card(self, env: str, card_id: int) -> int:
        """Закупка возобновлена: закрытие снимается, срок — снова потолок от загрузки.

        Args:
            env: Окружение внешней системы.
            card_id: ``risk_monitoring_contracts.id``.

        Returns:
            int: Число ссылок.
        """
        rows = await self.pool.fetch(
            """
            UPDATE ai_file_links
            SET closed_at = NULL, expires_at = linked_at + make_interval(days => $3)
            WHERE env = $1 AND rm_card_id = $2 AND closed_at IS NOT NULL
            RETURNING external_file_id
            """,
            env, int(card_id), Config.RM_TEXT_MAX_RETENTION_DAYS,
        )
        return len(rows)

    # ---------- документы ----------

    async def get_document_by_sha(self, content_sha256: str) -> Optional[asyncpg.Record]:
        """Документ по SHA-256 содержимого файла (дедупликация одинаковых файлов).

        Args:
            content_sha256: Хэш байтов файла.

        Returns:
            Optional[asyncpg.Record]: ``id``, ``text``, ``ocr_status`` или ``None``.
        """
        return await self.pool.fetchrow(
            "SELECT id, text, ocr_status FROM ai_documents WHERE content_sha256=$1",
            content_sha256,
        )

    async def get_document(self, document_id: int) -> Optional[asyncpg.Record]:
        """Документ по id.

        Args:
            document_id: ``ai_documents.id``.

        Returns:
            Optional[asyncpg.Record]: ``id``, ``text``, ``ocr_status``, ``text_purged_at`` или ``None``.
        """
        return await self.pool.fetchrow(
            "SELECT id, text, ocr_status, text_purged_at FROM ai_documents WHERE id=$1", document_id
        )

    async def upsert_document(
        self,
        content_sha256: str,
        text_sha256: Optional[str],
        file_ext: str,
        size_bytes: int,
        text: Optional[str],
        ocr_status: str = "done",
        ocr_error: Optional[str] = None,
        reader: Optional[str] = None,
    ) -> int:
        """Создаёт или обновляет документ по хэшу содержимого.

        Документ общий с экспертизами (``pe_documents.ai_document_id``): текст, записанный здесь, используется
        и ``/evaluate-documents`` (при ``DOC_DEDUP_ENABLED``).

        Args:
            content_sha256: SHA-256 байтов файла.
            text_sha256: SHA-256 извлечённого текста (поиск точных дублей между форматами).
            file_ext: Расширение файла.
            size_bytes: Размер файла.
            text: Извлечённый текст (``None``, если извлечь не удалось).
            ocr_status: ``done`` / ``failed``.
            ocr_error: Причина неудачи.
            reader: Способ чтения (``rm_pdf_text`` / ``file_reader``).

        Returns:
            int: ``ai_documents.id``.
        """
        # ON CONFLICT защищает от гонки: два воркера обрабатывают один и тот же файл
        return await self.pool.fetchval(
            """
            INSERT INTO ai_documents
                (content_sha256, text_sha256, file_ext, size_bytes, text, ocr_status, ocr_error, reader)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (content_sha256) DO UPDATE SET
                text_sha256 = EXCLUDED.text_sha256,
                text        = EXCLUDED.text,
                ocr_status  = EXCLUDED.ocr_status,
                ocr_error   = EXCLUDED.ocr_error,
                reader      = EXCLUDED.reader,
                text_purged_at = CASE WHEN EXCLUDED.text IS NOT NULL THEN NULL ELSE ai_documents.text_purged_at END,
                updated_at  = NOW()
            RETURNING id
            """,
            content_sha256, text_sha256, file_ext, size_bytes, strip_nul(text), ocr_status, ocr_error, reader,
        )

    # ---------- чанки и эмбеддинги ----------

    async def has_chunks(self, document_id: int, embedding_model: str) -> bool:
        """Есть ли у документа чанки РМ (нарезка ``rm_tokens_800``) с эмбеддингами указанной модели."""
        return bool(await self.pool.fetchval(
            f"SELECT 1 FROM ai_document_chunks WHERE document_id=$1 AND embedding_model=$2 AND {RM_CHUNKER_SQL} LIMIT 1",
            document_id, embedding_model,
        ))

    async def save_chunks(
        self,
        document_id: int,
        chunks: List[str],
        embeddings: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> None:
        """Заменяет чанки РМ (нарезка ``rm_tokens_800``) документа для модели эмбеддингов (в одной транзакции).

        Чанки экспертизы (``pages_v2``) того же документа не затрагиваются.

        Args:
            document_id: ``ai_documents.id``.
            chunks: Тексты чанков.
            embeddings: Векторы в порядке чанков.
            embedding_model: Название модели эмбеддингов.
        """
        rows = [
            (document_id, i, strip_nul(chunk), _vec(emb), embedding_model)
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"DELETE FROM ai_document_chunks WHERE document_id=$1 AND embedding_model=$2 AND {RM_CHUNKER_SQL}",
                    document_id, embedding_model,
                )
                await conn.executemany(
                    f"""
                    INSERT INTO ai_document_chunks
                        (document_id, chunk_no, text, embedding, embedding_model, chunker)
                    VALUES ($1, $2, $3, $4::vector, $5, '{RM_CHUNKER}')
                    """,
                    rows,
                )

    async def find_similar_chunks(
        self, embedding: Sequence[float], embedding_model: str, limit: int = 10
    ) -> List[asyncpg.Record]:
        """Семантический поиск (KPI-371…374): похожие фрагменты документов."""
        return await self.pool.fetch(
            """
            SELECT document_id, chunk_no, text,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM ai_document_chunks
            WHERE embedding_model = $2 AND chunker = 'rm_tokens_800'
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            _vec(embedding), embedding_model, limit,
        )

    async def find_similar_documents(
        self,
        document_id: int,
        embedding_model: str,
        env: str,
        min_similarity: float = 0.85,
        limit: int = 10,
        per_chunk: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Похожие документы: для каждого чанка исходного документа берём ближайшие чанки
        чужих документов, затем агрегируем по документам.
        coverage = доля чанков исходного документа, у которых нашёлся близкий аналог.
        """
        rows = await self.pool.fetch(
            """
            WITH src AS (
                SELECT chunk_no, embedding FROM ai_document_chunks
                WHERE document_id = $1 AND embedding_model = $2 AND chunker = 'rm_tokens_800'
            ),
            nn AS (
                SELECT s.chunk_no AS src_chunk, n.document_id,
                       1 - (n.embedding <=> s.embedding) AS sim
                FROM src s
                CROSS JOIN LATERAL (
                    SELECT c.document_id, c.embedding
                    FROM ai_document_chunks c
                    WHERE c.embedding_model = $2 AND c.document_id <> $1 AND c.chunker = 'rm_tokens_800'
                    ORDER BY c.embedding <=> s.embedding
                    LIMIT $6
                ) n
            ),
            best AS (
                SELECT document_id, src_chunk, MAX(sim) AS sim
                FROM nn WHERE sim >= $3
                GROUP BY document_id, src_chunk
            )
            SELECT b.document_id,
                   COUNT(*)::int AS matched_chunks,
                   (SELECT COUNT(*) FROM src)::int AS total_chunks,
                   AVG(b.sim)::float AS avg_similarity,
                   MAX(b.sim)::float AS max_similarity,
                   (SELECT COALESCE(array_agg(l.external_file_id), '{}')
                      FROM ai_file_links l
                     WHERE l.document_id = b.document_id AND l.env = $5) AS external_file_ids
            FROM best b
            GROUP BY b.document_id
            ORDER BY COUNT(*) DESC, AVG(b.sim) DESC
            LIMIT $4
            """,
            document_id, embedding_model, min_similarity, limit, env, per_chunk,
        )
        result = []
        for r in rows:
            total = r["total_chunks"] or 1
            result.append({
                "document_id": r["document_id"],
                "external_file_ids": list(r["external_file_ids"] or []),
                "coverage": round(r["matched_chunks"] / total, 3),
                "avg_similarity": round(r["avg_similarity"], 3),
                "max_similarity": round(r["max_similarity"], 3),
            })
        return result

    async def find_text_duplicates(self, document_id: int) -> List[int]:
        """Документы с идентичным извлечённым текстом (тот же текст в pdf и docx)."""
        rows = await self.pool.fetch(
            """
            SELECT d2.id FROM ai_documents d1
            JOIN ai_documents d2 ON d2.text_sha256 = d1.text_sha256 AND d2.id <> d1.id
            WHERE d1.id = $1 AND d1.text_sha256 IS NOT NULL
            """,
            document_id,
        )
        return [r["id"] for r in rows]

    # ---------- результаты анализа ----------

    async def get_analysis(
        self, document_id: int, analysis_type: str, prompt_version: str, model: str
    ) -> Optional[Dict[str, Any]]:
        """Сохранённый результат анализа документа (кэш по типу анализа, версии промпта и модели).

        Args:
            document_id: ``ai_documents.id``.
            analysis_type: Тип анализа (``doc_type``, ``data_extraction``, ``risk_analysis`` …).
            prompt_version: Хэш промпта и схемы.
            model: Модель LLM.

        Returns:
            Optional[Dict[str, Any]]: ``{"result", "compact", "confidence"}`` или ``None``.
        """
        row = await self.pool.fetchrow(
            """
            SELECT result, compact, confidence FROM ai_document_analysis
            WHERE document_id=$1 AND analysis_type=$2 AND prompt_version=$3 AND model=$4
            """,
            document_id, analysis_type, prompt_version, model,
        )
        if not row:
            return None
        return {
            "result": _load(row["result"]),
            "compact": _load(row["compact"]),
            "confidence": row["confidence"],
        }

    async def save_analysis(
        self,
        document_id: int,
        analysis_type: str,
        prompt_version: str,
        model: str,
        result: Dict[str, Any],
        compact: Dict[str, Any],
        confidence: Optional[float],
    ) -> None:
        """Сохраняет результат анализа (повторное сохранение перезаписывает).

        Args:
            document_id: ``ai_documents.id``.
            analysis_type: Тип анализа.
            prompt_version: Хэш промпта и схемы.
            model: Модель LLM.
            result: Полный результат (с evidence).
            compact: Компактный результат для внешней БД.
            confidence: Уверенность (если есть).
        """
        await self.pool.execute(
            """
            INSERT INTO ai_document_analysis
                (document_id, analysis_type, prompt_version, model, result, compact, confidence)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
            ON CONFLICT (document_id, analysis_type, prompt_version, model) DO UPDATE SET
                result = EXCLUDED.result,
                compact = EXCLUDED.compact,
                confidence = EXCLUDED.confidence,
                created_at = NOW()
            """,
            document_id, analysis_type, prompt_version, model,
            json.dumps(strip_nul(result), ensure_ascii=False, default=str),
            json.dumps(strip_nul(compact), ensure_ascii=False, default=str),
            confidence,
        )

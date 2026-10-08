import json
from typing import Any, Dict, List, Optional, Sequence

import asyncpg

from configs.config import Config
from configs.logger import get_logger

logger = get_logger(__name__)


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
        if self.pool:
            await self.pool.close()

    # ---------- ссылки внешний файл -> документ ----------

    async def get_link(self, env: str, external_file_id: int) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(
            "SELECT document_id FROM ai_file_links WHERE env=$1 AND external_file_id=$2",
            env, external_file_id,
        )

    async def link_file(self, env: str, external_file_id: int, document_id: int) -> None:
        await self.pool.execute(
            """
            INSERT INTO ai_file_links (env, external_file_id, document_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (env, external_file_id)
            DO UPDATE SET document_id = EXCLUDED.document_id, linked_at = NOW()
            """,
            env, external_file_id, document_id,
        )

    # ---------- документы ----------

    async def get_document_by_sha(self, content_sha256: str) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(
            "SELECT id, text, ocr_status FROM ai_documents WHERE content_sha256=$1",
            content_sha256,
        )

    async def get_document(self, document_id: int) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(
            "SELECT id, text, ocr_status FROM ai_documents WHERE id=$1", document_id
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
    ) -> int:
        # ON CONFLICT защищает от гонки: два воркера обрабатывают один и тот же файл
        return await self.pool.fetchval(
            """
            INSERT INTO ai_documents
                (content_sha256, text_sha256, file_ext, size_bytes, text, ocr_status, ocr_error)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (content_sha256) DO UPDATE SET
                text_sha256 = EXCLUDED.text_sha256,
                text        = EXCLUDED.text,
                ocr_status  = EXCLUDED.ocr_status,
                ocr_error   = EXCLUDED.ocr_error,
                updated_at  = NOW()
            RETURNING id
            """,
            content_sha256, text_sha256, file_ext, size_bytes, text, ocr_status, ocr_error,
        )

    # ---------- чанки и эмбеддинги ----------

    async def has_chunks(self, document_id: int, embedding_model: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM ai_document_chunks WHERE document_id=$1 AND embedding_model=$2 LIMIT 1",
            document_id, embedding_model,
        ))

    async def save_chunks(
        self,
        document_id: int,
        chunks: List[str],
        embeddings: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> None:
        rows = [
            (document_id, i, chunk, _vec(emb), embedding_model)
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM ai_document_chunks WHERE document_id=$1 AND embedding_model=$2",
                    document_id, embedding_model,
                )
                await conn.executemany(
                    """
                    INSERT INTO ai_document_chunks
                        (document_id, chunk_no, text, embedding, embedding_model)
                    VALUES ($1, $2, $3, $4::vector, $5)
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
            WHERE embedding_model = $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            _vec(embedding), embedding_model, limit,
        )

    # ---------- результаты анализа ----------

    async def get_analysis(
        self, document_id: int, analysis_type: str, prompt_version: str, model: str
    ) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(
            """
            SELECT result, compact, confidence FROM ai_document_analysis
            WHERE document_id=$1 AND analysis_type=$2 AND prompt_version=$3 AND model=$4
            """,
            document_id, analysis_type, prompt_version, model,
        )

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
            json.dumps(result, ensure_ascii=False, default=str),
            json.dumps(compact, ensure_ascii=False, default=str),
            confidence,
        )

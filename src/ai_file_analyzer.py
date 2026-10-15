import asyncio
import hashlib
import os
import tempfile
from typing import Any, Dict, List, Optional

import aiohttp

from configs.ai_repo import AIRepo
from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.file_reader import FileReader
from configs.http_client_manager import HTTPClientManager
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.parsing import CloudStorageParser
from configs.utils import split_large_text
from evaluate_documents.type_data_extractor import TypeDataExtractor
from configs.embedding_client import EmbeddingClient
from risk_monitoring.document_risk_analyzer import DocumentRiskAnalyzer
from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

logger = get_logger(__name__)

ANALYSIS_TYPE = "type_data_extractor"
RISK_TYPE = "risk_analysis"
EXTERNAL_TABLE = "risk_monitoring_files"
NO_TEXT_MARK = "[Нет читаемого текста]"
EMBED_CHUNK_TOKENS = 800      # размер чанка для эмбеддингов
LLM_CHUNK_TOKENS = 10000      # размер чанка для LLM (как в TasksPipeline)

SIMILAR_MIN_SIMILARITY = 0.85  # порог схожести чанков
SIMILAR_LIMIT = 10
NEAR_DUPLICATE_COVERAGE = 0.9  # доля совпавших чанков...
NEAR_DUPLICATE_AVG_SIM = 0.95  # ...при высокой средней схожести -> почти копия


class FileUnavailable(Exception):
    """Файл не удалось скачать (ожидаемая ситуация, без traceback в логах)."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_version() -> str:
    """Версия промпта = хэш файла. Изменили промпт -> кэш анализа инвалидируется сам."""
    with open("prompts/type_data_extractor_prompt.txt", "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


class AIFileAnalyzer:
    """
    Анализ файла из внешней БД (risk_monitoring_files) с кэшем по SHA-256 содержимого.

    Шаги: скачивание -> текст (OCR) -> эмбеддинги -> поиск похожих -> извлечение данных -> анализ рисков.
    Тяжёлые данные остаются в Postgres проекта, во внешний API уходит только компактный итог.
    """

    def __init__(self, http_manager: HTTPClientManager, environment: str, repo: AIRepo, push_results: bool = True):
        cfg = Config()
        db_config = cfg.get_database_config(environment)

        self.env = (environment or cfg.APP_ENV).lower()
        self.storage_path = db_config.storage_path
        self.http_manager = http_manager
        self.repo = repo
        self.push_results = push_results
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)

        client, self.model = get_llm()
        self.parser = CloudStorageParser(self.http_manager, None)
        self.file_reader = FileReader(client, self.model)
        self.extractor = TypeDataExtractor(client, self.model)
        self.risk_analyzer = DocumentRiskAnalyzer(client, self.model)
        self.risk_api = RiskMonitoringAPI(http_manager, environment)

        emb = cfg.get_embedding_config()
        self.embedding_model = emb.model
        self.embedder = EmbeddingClient(
            emb.api_url, emb.api_key, emb.model, batch_size=emb.batch_size, http_manager=http_manager
        )

        self.prompt_version = _prompt_version()
        self.risk_prompt_version = self.risk_analyzer.prompt_version
        self.semaphore = asyncio.Semaphore(3)

    # ------------------------------------------------------------------ вход

    async def fetch_files_meta(self, file_ids: List[int]) -> List[Dict[str, Any]]:
        """Метаданные файлов из внешней БД (только чтение)."""
        placeholders = ",".join("?" for _ in file_ids)
        sql = f"""
            SELECT 
                id, 
                CASE WHEN path IS NOT NULL THEN CONCAT(?, path) ELSE source_url END AS url, 
                CASE WHEN file_name LIKE CONCAT('%.', file_type) THEN file_name ELSE CONCAT(file_name, '.', file_type) END AS file_name, 
                CASE WHEN xml_source_type IS NOT NULL THEN xml_source_type ELSE owner_type END AS xml_source_type,
                owner_number
            FROM risk_monitoring_files WHERE id IN ({placeholders})
            """
        df = await self.data_fetcher.fetch_async_expertise_data(
            " ".join(sql.split()), bindings=[self.storage_path, *file_ids]
        )
        return df.to_dict("records") if not df.empty else []

    async def analyze_many(self, rows: List[Dict[str, Any]], force: bool = False) -> List[Dict[str, Any]]:
        async def _one(row):
            async with self.semaphore:
                try:
                    return await self.analyze_file(row, force)
                except FileUnavailable as e:
                    logger.warning(f"Файл {row.get('id')} недоступен: {e}")
                    return {"file_id": row.get("id"), "status": "error", "error_code": "download_failed", "error": str(e)}
                except Exception as e:
                    logger.error(f"Ошибка анализа файла {row.get('id')}: {e}", exc_info=True)
                    return {"file_id": row.get("id"), "status": "error", "error": str(e)}

        return await asyncio.gather(*(_one(r) for r in rows))

    # ------------------------------------------------------------ один файл

    async def analyze_file(self, row: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        file_id = int(row["id"])
        file_name = row.get("file_name") or f"file_{file_id}"

        # 1. Быстрый путь: файл уже связан с документом и анализ есть -> даже не скачиваем
        if not force:
            link = await self.repo.get_link(self.env, file_id)
            if link:
                cached = await self.repo.get_analysis(
                    link["document_id"], ANALYSIS_TYPE, self.prompt_version, self.model
                )
                if cached:
                    return self._response(file_id, link["document_id"], None, "cached", cached["compact"])

        # 2. Скачиваем и считаем хэш
        parse_result = await self.parser._download_http_file(
            url=row["url"],
            procurement_id=row.get("procurement_id"),
            source="risk_monitoring",
        )

        if parse_result["status"] != "success":
            raise FileUnavailable(parse_result.get("error", "неизвестная ошибка скачивания"))

        tmp_path = parse_result["file_path"]
        sha = parse_result["file_hash"]
        size = parse_result["file_size"]

        try:
            ext = os.path.splitext(file_name)[1].lower() or ".bin"

            # 3. Документ по хэшу (дедупликация: тот же файл у другого контракта)
            doc = await self.repo.get_document_by_sha(sha)
            if doc and doc["ocr_status"] == "done" and not force:
                document_id, text = doc["id"], doc["text"]
            else:
                text = await self.file_reader.read_file(tmp_path, file_name)
                if not text or NO_TEXT_MARK in text:
                    document_id = await self.repo.upsert_document(
                        sha, None, ext, size, None, ocr_status="failed", ocr_error="Не удалось извлечь текст"
                    )
                    await self.repo.link_file(self.env, file_id, document_id)
                    return {"file_id": file_id, "document_id": document_id, "content_sha256": sha,
                            "status": "error", "error": "Не удалось извлечь текст"}
                document_id = await self.repo.upsert_document(sha, _sha256_text(text), ext, size, text)

            await self.repo.link_file(self.env, file_id, document_id)

            # 4. Эмбеддинги (один раз на документ и модель эмбеддингов)
            if force or not await self.repo.has_chunks(document_id, self.embedding_model):
                await self._embed_and_save(document_id, text)

            # 5. LLM-анализ (один раз на документ + версию промпта + модель)
            if not force:
                cached = await self.repo.get_analysis(document_id, ANALYSIS_TYPE, self.prompt_version, self.model)
                if cached:
                    return self._response(file_id, document_id, sha, "cached", cached["compact"])

            llm_chunks = split_large_text(text, max_chunk_size=LLM_CHUNK_TOKENS)
            result = await self.extractor.extract_data_from_document(
                llm_chunks, file_name, doc_type="linkDocs", expertise_object=0
            )
            compact = self._build_compact(result)
            await self.repo.save_analysis(
                document_id, ANALYSIS_TYPE, self.prompt_version, self.model,
                result, compact, compact.get("confidence"),
            )
            return self._response(file_id, document_id, sha, "done", compact)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # --------------------------------------------------------------- helpers

    async def _embed_and_save(self, document_id: int, text: str) -> None:
        chunks = split_large_text(text, max_chunk_size=EMBED_CHUNK_TOKENS)
        if not chunks:
            return
        embs = await self.embedder.get_embeddings(chunks)
        # EmbeddingClient молча отбрасывает упавшие батчи -> длины могут не совпасть.
        # Сохранять с рассинхроном нельзя: эмбеддинг уедет к чужому чанку.
        if len(embs) != len(chunks):
            logger.warning(f"Эмбеддинги не сохранены для document_id={document_id}: {len(embs)} из {len(chunks)}")
            return
        await self.repo.save_chunks(document_id, chunks, embs.tolist(), self.embedding_model)

    @staticmethod
    def _build_compact(result: Dict[str, Any]) -> Dict[str, Any]:
        """Компактный итог для внешней БД: без текста, эмбеддингов и evidence."""
        tc = result.get("type_compliance", {}) or {}
        rd = result.get("readability", {}) or {}
        raw = result.get("raw_data", {}) or {}
        return {
            "detected_type": tc.get("detected_type"),
            "readability_status": rd.get("status"),
            "readability_score": rd.get("readability_score"),
            "document_name": (raw.get("document_name") or {}).get("value"),
            "summary": raw.get("summary"),
            "contract_number": (raw.get("contract_number") or {}).get("value"),
            "amounts": [f.get("value") for f in raw.get("finances", [])[:10]],
            "organizations": [
                {"role": o.get("role"), "name": o.get("name"), "inn": o.get("inn")}
                for o in raw.get("organizations", [])[:10]
            ],
            "law_references": [l.get("value") for l in raw.get("law_references", [])[:20]],
            "confidence": (raw.get("document_name") or {}).get("confidence"),
            "issues": (tc.get("issues") or []) + (rd.get("issues") or []),
        }

    @staticmethod
    def _response(file_id, document_id, sha, status, compact) -> Dict[str, Any]:
        return {
            "file_id": file_id,
            "document_id": document_id,
            "content_sha256": sha,
            "status": status,           # done / cached / error
            "ai_analysis": compact,     # именно это уходит во внешнюю БД
        }

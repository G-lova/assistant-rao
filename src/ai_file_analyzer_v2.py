import asyncio
import hashlib
import os
from typing import Any, Dict, List, Optional

from configs.ai_repo import AIRepo
from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.embedding_client import EmbeddingClient, EmbeddingError
from configs.file_reader import FileReader
from configs.http_client_manager import HTTPClientManager
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.parsing import CloudStorageParser
from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI
from configs.utils import split_large_text
from evaluate_documents.type_data_extractor import TypeDataExtractor
from risk_monitoring.document_risk_analyzer import DocumentRiskAnalyzer

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
        # storage_path передаём биндингом: подстановка f-строкой давала CONCAT(https://..., path) -> синтаксическая ошибка
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

        # 1. Быстрый путь: файл уже связан с документом и оба анализа есть -> файл не скачиваем.
        #    Похожие документы считаем заново: корпус растёт, результат поиска устаревает.
        if not force:
            link = await self.repo.get_link(self.env, file_id)
            if link:
                doc_id = link["document_id"]
                extraction = await self.repo.get_analysis(doc_id, ANALYSIS_TYPE, self.prompt_version, self.model)
                risk = await self.repo.get_analysis(doc_id, RISK_TYPE, self.risk_prompt_version, self.model)
                if extraction and risk:
                    similar = await self._find_similar(doc_id)
                    return await self._finalize(
                        file_id, doc_id, link["content_sha256"], "cached",
                        extraction["compact"], risk["compact"], similar,
                    )

        # 2. Скачиваем (хэш и размер возвращает ваш _download_http_file)
        parse_result = await self.parser._download_http_file(
            url=row["url"], procurement_id=None, source="risk_monitoring",
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

            # 4. Эмбеддинги и поиск похожих документов.
            #    Сбой эмбеддингов не должен ломать остальной анализ: просто не будет блока similar.
            embeddings_ok = await self._ensure_embeddings(document_id, text, force)
            similar = await self._find_similar(document_id) if embeddings_ok else []

            # 5. Извлечение данных (один раз на документ + версию промпта + модель)
            extraction = None if force else await self.repo.get_analysis(
                document_id, ANALYSIS_TYPE, self.prompt_version, self.model)
            extraction_cached = extraction is not None
            if extraction is None:
                llm_chunks = split_large_text(text, max_chunk_size=LLM_CHUNK_TOKENS)
                result = await self.extractor.extract_data_from_document(
                    llm_chunks, file_name, doc_type="linkDocs", expertise_object=0
                )
                compact = self._build_compact(result)
                await self.repo.save_analysis(
                    document_id, ANALYSIS_TYPE, self.prompt_version, self.model,
                    result, compact, compact.get("confidence"),
                )
                extraction = {"result": result, "compact": compact}

            # 6. Анализ рисков (отдельный кэш: смена risk-промпта не перезапускает экстракцию)
            risk = None if force else await self.repo.get_analysis(
                document_id, RISK_TYPE, self.risk_prompt_version, self.model)
            risk_cached = risk is not None
            if risk is None:
                context = {
                    "detected_type": extraction["compact"].get("detected_type"),
                    "summary": extraction["compact"].get("summary"),
                }
                risk_result = await self.risk_analyzer.analyze(text, file_name, context)
                risk_compact = self.risk_analyzer.to_compact(risk_result)
                # partial=True не кэшируем: при следующем запуске анализ будет повторён
                if not risk_result["partial"]:
                    await self.repo.save_analysis(
                        document_id, RISK_TYPE, self.risk_prompt_version, self.model,
                        risk_result, risk_compact, None,
                    )
                risk = {"result": risk_result, "compact": risk_compact}

            status = "cached" if (extraction_cached and risk_cached) else "done"
            return await self._finalize(
                file_id, document_id, sha, status, extraction["compact"], risk["compact"], similar
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # --------------------------------------------------------------- helpers

    async def _ensure_embeddings(self, document_id: int, text: str, force: bool) -> bool:
        """True, если у документа есть эмбеддинги (уже были или только что посчитаны)."""
        if not force and await self.repo.has_chunks(document_id, self.embedding_model):
            return True
        chunks = split_large_text(text, max_chunk_size=EMBED_CHUNK_TOKENS)
        if not chunks:
            return False
        try:
            embs = await self.embedder.get_embeddings(chunks)   # строгий: длина всегда == len(chunks)
        except EmbeddingError as e:
            logger.warning(f"Эмбеддинги не получены для document_id={document_id}: {e}")
            return False
        await self.repo.save_chunks(document_id, chunks, embs.tolist(), self.embedding_model)
        return True

    async def _find_similar(self, document_id: int) -> List[Dict[str, Any]]:
        """Похожие документы по эмбеддингам + точные дубли по тексту."""
        try:
            similar = await self.repo.find_similar_documents(
                document_id, self.embedding_model, self.env,
                min_similarity=SIMILAR_MIN_SIMILARITY, limit=SIMILAR_LIMIT,
            )
            exact = set(await self.repo.find_text_duplicates(document_id))
        except Exception as e:
            logger.warning(f"Поиск похожих документов не удался для document_id={document_id}: {e}")
            return []

        for s in similar:
            s["near_duplicate"] = (
                s["coverage"] >= NEAR_DUPLICATE_COVERAGE and s["avg_similarity"] >= NEAR_DUPLICATE_AVG_SIM
            )
            s["exact_text_duplicate"] = s["document_id"] in exact
        # точные дубли без чанков (эмбеддинги не считались) тоже показываем
        known = {s["document_id"] for s in similar}
        for d in exact - known:
            similar.append({"document_id": d, "external_file_ids": [], "coverage": 1.0, "avg_similarity": 1.0,
                            "max_similarity": 1.0, "near_duplicate": True, "exact_text_duplicate": True})
        return similar

    async def _finalize(self, file_id, document_id, sha, status, extraction, risk, similar) -> Dict[str, Any]:
        """Собирает итог и записывает его во внешний API (PATCH ai_analysis)."""
        ai_analysis = {
            **extraction,
            "risk_analysis": risk,
            "similar_documents": similar,
            "content_sha256": sha,
            "model": self.model,
            "prompt_versions": {"extraction": self.prompt_version, "risk": self.risk_prompt_version},
        }
        response = {
            "file_id": file_id,
            "document_id": document_id,
            "content_sha256": sha,
            "status": status,           # done / cached / error
            "ai_analysis": ai_analysis,
        }
        if self.push_results:
            try:
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, file_id, ai_analysis)
                response["pushed"] = True
            except Exception as e:
                # Анализ уже сохранён в нашей БД; повторный запуск возьмёт его из кэша и повторит только запись
                logger.error(f"Не удалось записать ai_analysis файла {file_id} во внешний API: {e}")
                response["pushed"] = False
                response["push_error"] = str(e)
        return response

    @staticmethod
    def _build_compact(result: Dict[str, Any]) -> Dict[str, Any]:
        """Компактный итог извлечения: без текста, эмбеддингов и evidence."""
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
"""Анализ файлов риск-мониторинга (``risk_monitoring_files``) → AI-паспорт документа во внешней БД.

Конвейер одного файла (концепция, гл. 18.2):

1. скачивание → SHA-256 → документ в ``ai_documents`` (дедупликация одинаковых файлов разных закупок);
2. текст (``FileReader``, OCR при необходимости) — один раз на содержимое;
3. чанки и эмбеддинги в pgvector → похожие документы (база знаний документов, гл. 18.13);
4. извлечение данных с доказательной базой (``DataExtractor``: организации, суммы, даты, нормы, ТРУ …);
5. тип документа (``TypeDetector`` + запасной вариант по названию);
6. AI-паспорт: заключение, назначение, структура, ключевые условия, тематика, технические объекты,
   итоговая оценка и рекомендации (``DocumentProfiler``);
7. признаки рисков по тексту с проверкой цитат (``DocumentRiskAnalyzer``);
8. сравнение с предыдущей редакцией: текстовый и смысловой diff, риски изменений, сопоставление рисков
   (что сохранилось, что появилось, что не выявлено в новой версии);
9. запись ``ai_analysis`` через ``PATCH /api/risk-monitoring/ai-analysis``.

Каждый шаг LLM кэшируется в ``ai_document_analysis`` по (документ, тип анализа, версия промпта, модель).

**Повторного анализа нет.** Файл, у которого во внешней БД уже ``pipeline.status = completed``,
пропускается. Повторно анализируются только новые версии документов (это новые строки файлов).
Перезапуск уже проанализированного файла — только в режиме отладки (``force`` при
``RISK_MONITORING_DEBUG=true``); ``recompute`` дополнительно игнорирует кэш LLM.
"""
import asyncio
import hashlib
import json
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from configs.config import Config
from configs.logger import get_logger
from risk_monitoring import passport, rm_sql, version_diff
from risk_monitoring.llm_json import prompt_version as _hash_version

logger = get_logger(__name__)

STAGE = "S1b"
EXTERNAL_TABLE = "risk_monitoring_files"
NO_TEXT_MARK = "[Нет читаемого текста]"
EMBED_CHUNK_TOKENS = 800
LLM_CHUNK_TOKENS = 10000
MAX_ATTEMPTS = 3

SIMILAR_MIN_SIMILARITY = 0.85
SIMILAR_LIMIT = 10
NEAR_DUPLICATE_COVERAGE = 0.9
NEAR_DUPLICATE_AVG_SIM = 0.95

CONTRACT_DOC_TYPES = {"docProjContractFiles", "docContractDoWorkFiles", "docContractNIRFiles", "docContractPostTovarFiles"}


class FileUnavailable(Exception):
    """Файл не удалось скачать (ожидаемая ситуация, без traceback в логах)."""


def sha256_file(path: str) -> str:
    """SHA-256 файла блоками.

    Args:
        path: Путь к файлу.

    Returns:
        str: Хэш в hex.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _file_version(*paths: str) -> str:
    """Версия промпта по файлам промпта и схемы (для кэша анализов, которые делают общие компоненты)."""
    parts = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                parts.append(f.read())
        except OSError:
            parts.append(p)
    return _hash_version(*parts)


def _json_list(value: Any) -> Optional[List[str]]:
    """Список кодов из ответа MySQL (``JSON_EXTRACT`` возвращает строку JSON или ``None``)."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    return [str(v) for v in data] if isinstance(data, list) else None


def _none(v: Any) -> Any:
    """NaN/NaT из pandas → ``None``, numpy-числа → Python-числа."""
    try:
        if v != v:  # NaN
            return None
    except Exception:  # noqa: BLE001
        pass
    return v.item() if hasattr(v, "item") else v


def purchase_context(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Краткий паспорт закупки из строки ``files_meta.sql`` (контекст для LLM и блок ``passport``).

    Args:
        row: Строка метаданных файла.

    Returns:
        Optional[Dict[str, Any]]: Номер, ФЗ, предмет, способ, НМЦК, цена, ОКПД2, заказчик, поставщик.
    """
    keys = ("purchase_number", "fz_type", "purchase_subject", "purchase_method", "purchase_nmck",
            "contract_price", "okpd2_code", "customer_name", "supplier_name")
    ctx = {k: _none(row.get(k)) for k in keys if _none(row.get(k)) not in (None, "", "null")}
    if ctx and row.get("risk_monitoring_contract_id") is not None:
        ctx["risk_monitoring_contract_id"] = int(row["risk_monitoring_contract_id"])
    return ctx or None


class AIFileAnalyzer:
    """Анализ файлов ``risk_monitoring_files`` и запись AI-паспорта во внешнюю БД.

    Args:
        http_manager: Общий ``HTTPClientManager``.
        environment: Окружение внешней системы (``X-API-Database``).
        repo: Открытый ``AIRepo`` (Postgres сервиса).
        push_results: Записывать ли результат во внешнюю БД.
        force: Анализировать уже проанализированные файлы (только в режиме отладки).
        recompute: Игнорировать кэш LLM (только в режиме отладки).
        concurrency: Сколько файлов обрабатывать одновременно.
    """

    def __init__(self, http_manager: Any, environment: str, repo: Any, push_results: bool = True,
                 force: bool = False, recompute: bool = False, concurrency: int = 3):
        """Создаёт компоненты конвейера (LLM, ридер, экстракторы, API внешней БД, эмбеддинги)."""
        from configs.data_fetcher import DataFetcher
        from configs.embedding_client import EmbeddingClient
        from configs.file_reader import FileReader
        from configs.llm_client import get_llm
        from configs.parsing import CloudStorageParser
        from evaluate_documents.data_extractor import DataExtractor
        from evaluate_documents.type_detector import TypeDetector
        from risk_monitoring.document_profiler import DocumentProfiler
        from risk_monitoring.document_risk_analyzer import DocumentRiskAnalyzer
        from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

        cfg = Config()
        db = cfg.get_database_config(environment)
        self.env = (environment or cfg.APP_ENV).lower()
        self.storage_path = db.storage_path
        self.repo = repo
        self.push_results = push_results
        self.force = force
        self.recompute = recompute
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)

        client, self.model = get_llm()
        self.parser = CloudStorageParser(http_manager, None)
        self.file_reader = FileReader(client, self.model)
        self.type_detector = TypeDetector(client, self.model)
        self.extractor = DataExtractor(client, self.model)
        self.profiler = DocumentProfiler(client, self.model)
        self.risk_analyzer = DocumentRiskAnalyzer(client, self.model)
        self.differ = version_diff.DocumentDiffAnalyzer(client, self.model)
        self.risk_api = RiskMonitoringAPI(http_manager, environment)

        emb = cfg.get_embedding_config()
        self.embedding_model = emb.model
        self.embedder = EmbeddingClient(emb.api_url, emb.api_key, emb.model, batch_size=emb.batch_size,
                                        http_manager=http_manager)
        self.versions = {
            "doc_type": _file_version("prompts/type_detector_prompt.txt", "schemas/type_detector_schema.json"),
            "data_extraction": _file_version("prompts/data_extractor_prompt.txt", "schemas/data_extractor_schema.json"),
            "document_profile": self.profiler.prompt_version,
            "risk_analysis": self.risk_analyzer.prompt_version,
            "version_diff": self.differ.prompt_version,
        }
        self.semaphore = asyncio.Semaphore(concurrency)

    # ------------------------------------------------------------------ вход

    async def fetch_files_meta(self, file_ids: List[int]) -> List[Dict[str, Any]]:
        """Метаданные файлов и паспорт закупки из внешней БД (только чтение).

        Args:
            file_ids: id файлов.

        Returns:
            List[Dict[str, Any]]: Строки ``files_meta.sql``.
        """
        sql, bindings = rm_sql.render("files_meta.sql", head=[self.storage_path], ids=file_ids)
        df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [{k: _none(v) for k, v in r.items()} for r in df.to_dict("records")] if not df.empty else []

    async def fetch_previous_versions(self, file_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Предыдущие версии файлов (тот же файл той же закупки с меньшей версией XML).

        Args:
            file_ids: id файлов.

        Returns:
            Dict[int, Dict[str, Any]]: ``current_id → строка предыдущей версии`` (максимальная версия).
        """
        sql, bindings = rm_sql.render("files_previous.sql", head=[self.storage_path], ids=file_ids)
        try:
            df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        except Exception as e:  # noqa: BLE001 — без предыдущей версии анализ всё равно возможен
            logger.warning(f"Предыдущие версии файлов не получены: {e}")
            return {}
        best: Dict[int, Dict[str, Any]] = {}
        for r in (df.to_dict("records") if not df.empty else []):
            r = {k: _none(v) for k, v in r.items()}
            cur = int(r["current_id"])
            if cur not in best or (r.get("eis_version") or 0) > (best[cur].get("eis_version") or 0):
                best[cur] = r
        return best

    async def analyze_ids(self, file_ids: List[int]) -> List[Dict[str, Any]]:
        """Анализирует файлы по id.

        Args:
            file_ids: id файлов ``risk_monitoring_files``.

        Returns:
            List[Dict[str, Any]]: По одному результату на id: ``status`` (``done`` / ``skipped`` /
            ``not_found`` / ``error``), ``pushed``, ``ai_analysis`` (кроме пропущенных).
        """
        ids = list(dict.fromkeys(int(i) for i in file_ids))
        rows = await self.fetch_files_meta(ids)
        found = {int(r["id"]): r for r in rows}
        previous = await self.fetch_previous_versions(list(found)) if found else {}
        results = await asyncio.gather(*(self._guarded(found[i], previous.get(i)) for i in ids if i in found))
        missing = [{"file_id": i, "status": "not_found"} for i in ids if i not in found]
        return list(results) + missing

    async def _guarded(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Обёртка одного файла: семафор, правило «без повторного анализа», обработка ошибок."""
        file_id = int(row["id"])
        attempt = int(row.get("rm_attempt") or 0) + 1
        if not self.force:
            if row.get("rm_status") == "completed":
                return {"file_id": file_id, "status": "skipped", "reason": "already_analyzed"}
            if row.get("rm_status") == "failed" and attempt > MAX_ATTEMPTS:
                return {"file_id": file_id, "status": "skipped", "reason": "max_attempts"}
        async with self.semaphore:
            try:
                return await self.analyze_file(row, previous, attempt)
            except FileUnavailable as e:
                logger.warning(f"Файл {file_id} недоступен: {e}")
                return await self._push_failure(file_id, attempt, f"download_failed: {e}")
            except Exception as e:  # noqa: BLE001 — сбой одного файла не останавливает остальные
                logger.error(f"Ошибка анализа файла {file_id}: {e}", exc_info=True)
                return await self._push_failure(file_id, attempt, f"{type(e).__name__}: {e}")

    async def _push_failure(self, file_id: int, attempt: int, error: str) -> Dict[str, Any]:
        """Записывает ``pipeline.status = failed`` (кроме режима отладки, чтобы не затереть готовый анализ)."""
        result = {"file_id": file_id, "status": "error", "error": error, "pushed": False}
        if self.push_results and not self.force:
            block = {"pipeline": passport.pipeline_block(STAGE, "failed", self.model, self.versions,
                                                         attempt=attempt, error=error)}
            try:
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, file_id, block)
                result["pushed"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось записать статус ошибки файла {file_id}: {e}")
        return result

    # ------------------------------------------------------------ один файл

    async def analyze_file(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]], attempt: int = 1) -> Dict[str, Any]:
        """Полный анализ одного файла и запись паспорта.

        Args:
            row: Строка ``files_meta.sql``.
            previous: Предыдущая версия файла (``files_previous.sql``) или ``None``.
            attempt: Номер попытки.

        Returns:
            Dict[str, Any]: ``file_id``, ``document_id``, ``status``, ``pushed``, ``ai_analysis``.

        Raises:
            FileUnavailable: Файл не скачался.
        """
        file_id = int(row["id"])
        file_name = row.get("file_name") or f"file_{file_id}"
        if not row.get("url"):
            raise FileUnavailable("нет пути к файлу и ссылки ЕИС")
        purchase = purchase_context(row)
        prev_id = int(previous["id"]) if previous else None

        document_id, text, sha, size = await self._load_document(file_id, row["url"], file_name)
        if text is None:
            pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions, prev_id,
                                               self.force, attempt, result="no_text")
            ai = passport.no_text_analysis(row, "Не удалось извлечь текст документа", pipeline, self.model, sha)
            return await self._finalize(file_id, document_id, ai)

        from configs.utils import split_large_text
        from knowledge_store.pages import PAGE_MARKER_RE

        embeddings_ok = await self._ensure_embeddings(document_id, text)
        similar = await self._find_similar(document_id) if embeddings_ok else []

        chunks = split_large_text(text, max_chunk_size=LLM_CHUNK_TOKENS) or [text]
        extraction = await self._cached(document_id, "data_extraction",
                                        lambda: self.extractor.extract_data_from_document(chunks, file_name),
                                        keep=lambda r: isinstance(r, dict) and bool(r.get("raw_data")))
        if isinstance(extraction, str):
            extraction = json.loads(extraction)
        type_info, type_decode = await self._doc_type(document_id, chunks, file_name, extraction, row)
        summary = ((extraction or {}).get("raw_data") or {}).get("summary")

        profile = await self._cached(document_id, "document_profile",
                                     lambda: self.profiler.profile(text, file_name, type_decode, summary, purchase))
        risk_context = {"detected_type": type_info.get("detected_type"), "type_decode": type_decode,
                        "summary": summary, "purchase": purchase}
        risk = await self._cached(document_id, "risk_analysis",
                                  lambda: self.risk_analyzer.analyze(text, file_name, risk_context),
                                  compact=self.risk_analyzer.to_compact,
                                  keep=lambda r: not r.get("partial"))
        risk_compact = self.risk_analyzer.to_compact(risk) if risk else {}

        version_changes, version_risks = None, []
        if previous:
            version_changes, version_risks = await self._compare_versions(
                previous, document_id, sha, text, file_name, type_decode)

        pages = len({m.group(1) for m in PAGE_MARKER_RE.finditer(text)}) or None
        ocr_used = "(OCR)" in text
        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions, prev_id, self.force, attempt)
        ai = passport.build_file_analysis(
            file_meta=row, type_info=type_info, type_decode=type_decode, extraction=extraction, profile=profile,
            risk_compact=risk_compact, extra_risks=version_risks, similar=similar, version_changes=version_changes,
            previous_codes=_json_list(previous.get("rule_ids")) if previous else None, pages=pages,
            text_chars=len(text), ocr_used=ocr_used, purchase=purchase, pipeline=pipeline, model=self.model,
            content_sha256=sha,
        )
        return await self._finalize(file_id, document_id, ai)

    async def _load_document(self, file_id: int, url: str, file_name: str):
        """Скачивает файл, находит или создаёт документ по хэшу содержимого и связывает его с файлом.

        Args:
            file_id: id внешнего файла.
            url: Ссылка на файл (хранилище внешней системы или ЕИС).
            file_name: Имя файла.

        Returns:
            tuple: ``(document_id, text | None, sha256, size)``; ``text = None`` — текст не извлечён.

        Raises:
            FileUnavailable: Файл не скачался.
        """
        res = await self.parser._download_http_file(url=url, procurement_id=None, source="risk_monitoring",
                                                     original_filename=file_name)
        if not res or res.get("status") != "success":
            raise FileUnavailable((res or {}).get("error", "неизвестная ошибка скачивания"))
        tmp_path = res["file_path"]
        try:
            sha = sha256_file(tmp_path)
            size = os.path.getsize(tmp_path)
            ext = os.path.splitext(file_name)[1].lower() or res.get("file_extension") or ".bin"
            doc = await self.repo.get_document_by_sha(sha)
            if doc and doc["ocr_status"] == "done" and doc["text"] and not self.recompute:
                document_id, text = doc["id"], doc["text"]
            else:
                text = await self.file_reader.read_file(tmp_path, file_name)
                if not text or not text.strip() or NO_TEXT_MARK in text:
                    document_id = await self.repo.upsert_document(sha, None, ext, size, None, ocr_status="failed",
                                                                  ocr_error="Не удалось извлечь текст")
                    await self.repo.link_file(self.env, file_id, document_id)
                    return document_id, None, sha, size
                text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                document_id = await self.repo.upsert_document(sha, text_sha, ext, size, text)
            await self.repo.link_file(self.env, file_id, document_id)
            return document_id, text, sha, size
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def _cached(self, document_id: int, analysis_type: str, produce: Callable[[], Awaitable[Any]],
                      compact: Optional[Callable[[Any], Any]] = None,
                      keep: Optional[Callable[[Any], bool]] = None, version_key: Optional[str] = None) -> Any:
        """Результат анализа из кэша ``ai_document_analysis`` или новый (с сохранением).

        Args:
            document_id: ``ai_documents.id``.
            analysis_type: Тип анализа (ключ кэша).
            produce: Корутина-фабрика нового результата.
            compact: Функция компактного вида (по умолчанию — сам результат).
            keep: Условие сохранения в кэш (например, не кэшировать неполный риск-анализ).
            version_key: Ключ версии промпта в ``self.versions`` (по умолчанию — ``analysis_type``).

        Returns:
            Any: Результат (``None``, если анализ не дал результата).
        """
        version = self.versions[version_key or analysis_type]
        if not self.recompute:
            cached = await self.repo.get_analysis(document_id, analysis_type, version, self.model)
            if cached:
                return cached["result"]
        result = await produce()
        if result and (keep is None or keep(result)):
            await self.repo.save_analysis(document_id, analysis_type, version, self.model, result,
                                          compact(result) if compact else result,
                                          result.get("confidence") if isinstance(result, dict) and isinstance(result.get("confidence"), (int, float)) else None)
        return result

    async def _doc_type(self, document_id: int, chunks: List[str], file_name: str,
                        extraction: Optional[Dict[str, Any]], row: Dict[str, Any]):
        """Тип документа: ``TypeDetector`` по первому чанку, запасной вариант — по названию из извлечения.

        Проект контракта в составе извещения (владелец не контракт) приводится к ``docProjContractFiles``.

        Returns:
            tuple: ``(type_info, type_decode)``.
        """
        from evaluate_documents.type_data_extractor import ALLOWED_DOC_TYPES, DOCUMENT_TYPE_MAPPING

        info = await self._cached(document_id, "doc_type",
                                  lambda: self.type_detector.detect_document_type(content=chunks[0], filename=file_name))
        info = dict(info or {})
        detected = info.get("detected_type") or ""
        if detected not in ALLOWED_DOC_TYPES:
            name = ((extraction or {}).get("raw_data") or {}).get("document_name") or ""
            if isinstance(name, dict):
                name = name.get("value") or ""
            detected = self.type_detector.detect_document_type_by_name(name or file_name) or "docDopMaterialsFiles"
        if (row.get("owner_type") or "") != "contract" and detected in CONTRACT_DOC_TYPES:
            detected = "docProjContractFiles"
        info["detected_type"] = detected
        return info, DOCUMENT_TYPE_MAPPING.get(detected)

    async def _compare_versions(self, previous: Dict[str, Any], document_id: int, sha: str, text: str,
                                file_name: str, type_decode: Optional[str]):
        """Сравнение с предыдущей редакцией: текстовый diff + смысловой diff LLM + риск DOC-011.

        Текст предыдущей версии берётся из ``ai_documents`` (она уже анализировалась); если связи нет —
        файл скачивается и читается без LLM-анализа.

        Returns:
            tuple: ``(version_changes, risks)``.
        """
        prev_id = int(previous["id"])
        base = {"previous_file_id": prev_id, "previous_eis_version": previous.get("eis_version")}
        if previous.get("content_sha256") and previous["content_sha256"] == sha:
            return {**base, "identical": True, "similarity": 1.0}, []
        prev_doc_id, prev_text = None, None
        link = await self.repo.get_link(self.env, prev_id)
        if link:
            prev_doc_id = link["document_id"]
            if link["content_sha256"] == sha:
                return {**base, "identical": True, "similarity": 1.0}, []
            doc = await self.repo.get_document(prev_doc_id)
            prev_text = doc["text"] if doc else None
        elif previous.get("url"):
            try:
                prev_doc_id, prev_text, prev_sha, _ = await self._load_document(prev_id, previous["url"],
                                                                                previous.get("file_name") or file_name)
                if prev_sha == sha:
                    return {**base, "identical": True, "similarity": 1.0}, []
            except FileUnavailable as e:
                return {**base, "error": f"Предыдущая версия недоступна: {e}"}, []
        if not prev_text:
            return {**base, "error": "Нет текста предыдущей версии"}, []

        diff = version_diff.text_diff(prev_text, text)
        result = {**base, "identical": diff["identical"], "similarity": diff["similarity"], "stats": diff["stats"],
                  "fragments_total": diff.get("fragments_total", 0)}
        if diff["identical"]:
            return result, []
        try:
            llm = await self._cached(document_id, f"version_diff:{prev_doc_id}",
                                     lambda: self.differ.analyze(diff, file_name, type_decode),
                                     version_key="version_diff")
        except Exception as e:  # noqa: BLE001 — без смыслового diff остаётся текстовый
            logger.error(f"Смысловое сравнение редакций файла не удалось: {e}")
            llm = None
        if llm:
            result.update({"summary": llm.get("summary"), "risk_relevant": bool(llm.get("risk_relevant")),
                           "changes": llm.get("changes") or []})
        else:
            result["fragments"] = diff["fragments"][:10]
        return result, version_diff.version_risks(llm, prev_id)

    # --------------------------------------------------------------- эмбеддинги

    async def _ensure_embeddings(self, document_id: int, text: str) -> bool:
        """Есть ли у документа эмбеддинги (уже были или посчитаны сейчас). Сбой не ломает анализ."""
        from configs.embedding_client import EmbeddingError
        from configs.utils import split_large_text

        if not self.recompute and await self.repo.has_chunks(document_id, self.embedding_model):
            return True
        chunks = split_large_text(text, max_chunk_size=EMBED_CHUNK_TOKENS)
        if not chunks:
            return False
        try:
            embs = await self.embedder.get_embeddings(chunks, strict=True)
        except EmbeddingError as e:
            logger.warning(f"Эмбеддинги не получены для document_id={document_id}: {e}")
            return False
        await self.repo.save_chunks(document_id, chunks, embs.tolist(), self.embedding_model)
        return True

    async def _find_similar(self, document_id: int) -> List[Dict[str, Any]]:
        """Похожие документы по эмбеддингам + точные дубли по тексту (с признаком почти-копии)."""
        try:
            similar = await self.repo.find_similar_documents(
                document_id, self.embedding_model, self.env, min_similarity=SIMILAR_MIN_SIMILARITY, limit=SIMILAR_LIMIT)
            exact = set(await self.repo.find_text_duplicates(document_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Поиск похожих документов не удался для document_id={document_id}: {e}")
            return []
        for s in similar:
            s["near_duplicate"] = s["coverage"] >= NEAR_DUPLICATE_COVERAGE and s["avg_similarity"] >= NEAR_DUPLICATE_AVG_SIM
            s["exact_text_duplicate"] = s["document_id"] in exact
        known = {s["document_id"] for s in similar}
        for d in exact - known:
            similar.append({"document_id": d, "external_file_ids": [], "coverage": 1.0, "avg_similarity": 1.0,
                            "max_similarity": 1.0, "near_duplicate": True, "exact_text_duplicate": True})
        return similar

    # ------------------------------------------------------------------ запись

    async def _finalize(self, file_id: int, document_id: Optional[int], ai_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Записывает ``ai_analysis`` во внешнюю БД (если включено) и возвращает итог по файлу."""
        response = {"file_id": file_id, "document_id": document_id, "status": "done",
                    "pushed": False, "ai_analysis": ai_analysis}
        if self.push_results:
            try:
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, file_id, ai_analysis)
                response["pushed"] = True
            except Exception as e:  # noqa: BLE001 — анализ сохранён в кэше, повтор запишет его без LLM
                logger.error(f"Не удалось записать ai_analysis файла {file_id}: {e}")
                response["push_error"] = str(e)
        return response

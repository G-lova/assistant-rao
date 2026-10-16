"""Загрузка типовых положений о закупке из ЕИС: запрос по региону и дате, разбор, чтение файлов, нарезка по пунктам.

Цепочка для одного XML ``typalOrderClause``:

1. :func:`typal_clauses.parse_typal_xml` → версии положения;
2. :meth:`TypalRepo.save_clause` — версия, заказчики, файлы;
3. файлы-положения, перечни и приказы скачиваются по ссылкам ЕИС и читаются (``FileReader``: .doc, .docx, PDF,
   сканы — через OCR); одинаковый файл (``contentUid``) в разных версиях читается один раз;
4. текст сохраняется общим документом (``ai_documents``), положения нарезаются по пунктам с эмбеддингами.

Регион — код ФНС (две цифры: «34» — Волгоградская область). Если ``settings.TYPAL_REGIONS`` пуст, регионы берутся
из закупок заказчиков на мониторинге (``dataset.owner.region``) окружения задачи.
"""
import hashlib
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from configs.logger import get_logger
from risk_monitoring import settings, typal_clauses
from risk_monitoring.typal_clauses import TypalRepo

logger = get_logger(__name__)


def lookback_dates(days: int, today: Optional[date] = None) -> List[str]:
    """Даты выгрузки за последние ``days`` дней (без сегодняшнего: ЕИС выгружает прошедшие сутки)."""
    today = today or date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(1, max(1, days) + 1)]


class TypalLoader:
    """Загрузка и обновление типовых положений.

    Args:
        http_manager: ``HTTPClientManager``.
        repo: ``AIRepo`` (пул Postgres, документы).
        environment: Окружение (только для поиска регионов заказчиков на мониторинге).
        read_files: Скачивать и читать файлы положений.
    """

    def __init__(self, http_manager: Any, repo: Any, environment: Optional[str] = None, read_files: bool = True):
        """Создаёт парсер ЕИС, ридер файлов и клиент эмбеддингов."""
        from configs.config import Config
        from configs.eis_parsing import EISParser

        self.http_manager = http_manager
        self.repo = repo
        self.typal = TypalRepo(repo.pool)
        self.environment = environment
        self.read_files = read_files
        self.eis = EISParser(http_manager)
        self._reader = None
        emb = Config().get_embedding_config()
        self.embedding_model = emb.model
        self._emb_cfg = emb
        self.stats: Dict[str, int] = {"xml": 0, "versions": 0, "files_read": 0, "files_failed": 0, "chunks": 0}

    def _file_reader(self):
        """``FileReader`` с устойчивым клиентом модели (OCR сканов) — создаётся при первом файле."""
        if self._reader is None:
            from configs.file_reader import FileReader
            from configs.llm_client import get_llm
            from configs.llm_resilience import ResilientClient

            client, model = get_llm()
            self._reader = FileReader(ResilientClient(client, retries=settings.LLM_PROXY_RETRIES), model)
        return self._reader

    # ------------------------------------------------------------------ регионы и запрос в ЕИС
    async def regions(self) -> List[str]:
        """Коды регионов: ``settings.TYPAL_REGIONS`` или регионы заказчиков на мониторинге окружения."""
        if settings.TYPAL_REGIONS:
            return [str(r).zfill(2) for r in settings.TYPAL_REGIONS]
        if not self.environment:
            return []
        from configs.config import Config
        from configs.data_fetcher import DataFetcher
        from risk_monitoring import rm_sql

        db = Config().get_database_config(self.environment)
        sql, b = rm_sql.render("monitored_regions.sql")
        try:
            df = await DataFetcher(db.url, db.headers, self.http_manager).fetch_async_expertise_data(sql, bindings=b)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Регионы заказчиков не получены: {type(e).__name__}: {e}")
            return []
        return sorted({str(r).strip().zfill(2) for r in df.get("region", []) if str(r or "").strip().isdigit()})

    async def load_region(self, region: str, exact_date: str) -> List[Dict[str, Any]]:
        """Типовые положения региона, опубликованные или изменённые за дату.

        Args:
            region: Код региона ФНС.
            exact_date: Дата ``ГГГГ-ММ-ДД``.

        Returns:
            List[Dict[str, Any]]: Результаты по версиям (:meth:`load_xml`).
        """
        res = await self.eis._parse_eis_soap_ip("getDocsByOrgRegionRequest", subsystem_type=settings.TYPAL_SUBSYSTEM,
                                                org_region=str(region), fz=223, document_type=settings.TYPAL_DOCUMENT_TYPE,
                                                exact_date=exact_date)
        if res.get("status") != "success":
            err = str(res.get("error") or "")
            # «нет данных за дату» — обычный ответ ЕИС, а не сбой
            level = logger.info if "не найден" in err.lower() or "нет данных" in err.lower() else logger.warning
            level(f"Типовые положения {region} за {exact_date}: {err[:300]}")
            return []
        out = []
        for f in res.get("files") or []:
            path = f.get("file_path")
            if not path or not str(path).lower().endswith(".xml"):
                continue
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                out += await self.load_xml(raw, source=f.get("filename"))
            except ValueError as e:
                logger.info(f"{f.get('filename')}: не типовое положение ({e})")
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        return out

    # ------------------------------------------------------------------ разбор и сохранение
    async def load_xml(self, raw: bytes, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """Сохраняет версии положения из XML и читает его файлы.

        Args:
            raw: Байты XML ``typalOrderClause``.
            source: Имя файла (для журнала).

        Returns:
            List[Dict[str, Any]]: ``registration_number``, ``version``, ``clause_id``, ``customers``, ``files``.

        Raises:
            ValueError: Это не ``typalOrderClause``.
        """
        clauses = typal_clauses.parse_typal_xml(raw)
        self.stats["xml"] += 1
        out = []
        for c in clauses:
            cid = await self.typal.save_clause(c)
            self.stats["versions"] += 1
            files = await self._read_files(cid) if self.read_files else []
            logger.info(f"Типовое положение {c['registration_number']} v{c['version']} ({source}): заказчиков "
                        f"{len(c['customers'])}, файлов {len(c['files'])}, прочитано {sum(1 for f in files if f['status'] == 'done')}")
            out.append({"registration_number": c["registration_number"], "version": c["version"], "clause_id": cid,
                        "name": c.get("name"), "use_for_base_date": c.get("use_for_base_date"),
                        "customers": len(c["customers"]), "files": files})
        return out

    async def _read_files(self, clause_id: int) -> List[Dict[str, Any]]:
        """Скачивает и читает файлы версии без текста; положения режет по пунктам."""
        out = []
        for f in await self.typal.files_to_read(clause_id):
            try:
                doc_id, chunks = await self._read_one(f)
                status = "done" if doc_id else "no_text"
                await self.typal.set_file_document(f["id"], doc_id, status)
                self.stats["files_read"] += 1
                out.append({"file_name": f["file_name"], "kind": f["kind"], "status": status, "chunks": chunks})
            except Exception as e:  # noqa: BLE001 — сбой одного файла не останавливает остальные
                logger.warning(f"Файл типового положения {f['file_name']}: {type(e).__name__}: {e}")
                await self.typal.set_file_document(f["id"], None, "failed", f"{type(e).__name__}: {e}"[:500])
                self.stats["files_failed"] += 1
                out.append({"file_name": f["file_name"], "kind": f["kind"], "status": "failed", "error": str(e)[:300]})
        return out

    async def _read_one(self, f: Dict[str, Any]):
        """Один файл: скачать, прочитать (или взять готовый текст по хэшу), сохранить, нарезать положение.

        Returns:
            tuple: ``(document_id | None, число чанков)``.
        """
        res = await self.eis._download_eis_file(file_url=f["url"], procurement_id=None)
        if not res or res.get("status") != "success":
            raise RuntimeError((res or {}).get("error") or "файл не скачан")
        path = res["file_path"]
        try:
            with open(path, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            size = os.path.getsize(path)
            ext = os.path.splitext(f["file_name"] or "")[1].lower() or res.get("file_extension") or ".bin"
            doc = await self.repo.get_document_by_sha(sha)
            if doc and doc["text"]:
                doc_id, text = int(doc["id"]), doc["text"]
            else:
                text = await self._file_reader().read_file(path, f["file_name"])
                if not text or not text.strip():
                    await self.repo.upsert_document(sha, None, ext, size, None, ocr_status="failed",
                                                    ocr_error="Не удалось извлечь текст")
                    return None, 0
                doc_id = await self.repo.upsert_document(sha, hashlib.sha256(text.encode("utf-8")).hexdigest(), ext,
                                                         size, text, reader="file_reader")
        finally:
            if os.path.exists(path):
                os.unlink(path)
        chunks = 0
        if f["kind"] == "position":
            chunks = await self._index_points(int(doc_id), text)
        return int(doc_id), chunks

    async def _index_points(self, document_id: int, text: str) -> int:
        """Нарезка положения по пунктам и эмбеддинги (без эмбеддингов чанки всё равно сохраняются)."""
        points = typal_clauses.split_points(text)
        vectors: Sequence[Optional[Sequence[float]]] = [None] * len(points)
        try:
            from configs.embedding_client import EmbeddingClient

            e = self._emb_cfg
            emb = EmbeddingClient(e.api_url, e.api_key, e.model, batch_size=e.batch_size, http_manager=self.http_manager)
            vectors = list(await emb.get_embeddings([p["text"] for p in points], strict=True))
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"Эмбеддинги положения {document_id} не получены: {type(ex).__name__}: {ex}")
        await self.typal.save_point_chunks(document_id, points, vectors, self.embedding_model)
        self.stats["chunks"] += len(points)
        return len(points)

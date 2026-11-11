"""Загрузка типовых положений о закупке из ЕИС: запрос по региону и дате, разбор, чтение файлов, нарезка по пунктам.

Цепочка для одного XML ``typalOrderClause``:

1. :func:`typal_clauses.parse_typal_xml` → версии положения;
2. :meth:`TypalRepo.save_clause` — версия, заказчики, файлы;
3. файлы-положения, перечни и приказы скачиваются по ссылкам ЕИС и читаются (``FileReader``: .doc, .docx, PDF,
   сканы — через OCR); одинаковый файл (``contentUid``) в разных версиях читается один раз;
4. текст сохраняется общим документом (``ai_documents``), положения нарезаются по пунктам с эмбеддингами.

Регион — код ФНС (две цифры: «34» — Волгоградская область). Если ``settings.TYPAL_REGIONS`` пуст, регионы берутся
из закупок заказчиков на мониторинге (``dataset.owner.region``) окружения задачи.

Положения заказчиков (``orderClause``, подсистема ``settings.ORDER_CLAUSE_SUBSYSTEM``) загружаются тем же запросом
по региону и дате (``load_region(..., kind="customer")``). Их в регионе за день много, поэтому при
``settings.ORDER_CLAUSE_ONLY_MONITORED`` сохраняются и читаются только положения заказчиков на мониторинге (ИНН —
``queries/rm/monitored_customer_inns.sql``); остальные пропускаются (``stats.skipped_not_monitored``).
"""
import hashlib
import asyncio
import re
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from configs.logger import get_logger
from configs import settings
from risk_monitoring import progress, typal_clauses
from risk_monitoring.typal_clauses import TypalRepo

logger = get_logger(__name__)

# Лимит запросов ЕИС («Код 13: Превышено количество допустимых запросов к сервису») и ответ «нет документов» (нет
# архива — обычный ответ на регион и дату без новых положений)
EIS_LIMIT_RE = re.compile(r"код\s*13|превышено\s+количество\s+допустимых\s+запросов", re.IGNORECASE)
NO_DATA_RE = re.compile(r"не\s+найден|нет\s+данных|url\s+архива", re.IGNORECASE)


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
        self.stats: Dict[str, int] = {"xml": 0, "versions": 0, "files_read": 0, "files_failed": 0, "chunks": 0,
                                      "skipped_not_monitored": 0}
        self.only_inns: Optional[set] = None   # фильтр положений заказчиков (monitored_inns)
        # дозагрузка за период: положения заказчиков вне only_inns сохраняются без файлов (реквизиты дешёвые и
        # пригодятся — новые организации, сверка), а не пропускаются
        self.keep_others_metadata = False

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

    async def monitored_inns(self) -> set:
        """ИНН заказчиков на мониторинге окружения (фильтр положений заказчиков); пусто — фильтр не применяется."""
        if not self.environment:
            return set()
        from configs.config import Config
        from configs.data_fetcher import DataFetcher
        from risk_monitoring import rm_sql

        db = Config().get_database_config(self.environment)
        sql, b = rm_sql.render("monitored_customer_inns.sql")
        try:
            df = await DataFetcher(db.url, db.headers, self.http_manager).fetch_async_expertise_data(sql, bindings=b)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ИНН заказчиков на мониторинге не получены: {type(e).__name__}: {e}")
            return set()
        return {str(v).strip() for v in df.get("inn", []) if str(v or "").strip()}

    async def _eis_pause(self) -> None:
        """Не чаще одного запроса к ЕИС в ``TYPAL_EIS_MIN_INTERVAL_SEC`` секунд (лимит ЕИС — 90 запросов в минуту
        на всех, кто ходит с этого адреса)."""
        import time

        last = self.__dict__.get("_eis_last")
        if last is not None:
            wait = settings.TYPAL_EIS_MIN_INTERVAL_SEC - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._eis_last = time.monotonic()

    async def load_region(self, region: str, exact_date: str, kind: str = "typal") -> List[Dict[str, Any]]:
        """Типовые положения (или положения заказчиков) региона, опубликованные или изменённые за дату.

        Args:
            region: Код региона ФНС.
            exact_date: Дата ``ГГГГ-ММ-ДД``.
            kind: ``typal`` — ``typalOrderClause``; ``customer`` — ``orderClause``.

        Returns:
            List[Dict[str, Any]]: Результаты по версиям (:meth:`load_xml`).
        """
        subsystem, doc_type, label = ((settings.ORDER_CLAUSE_SUBSYSTEM, settings.ORDER_CLAUSE_DOCUMENT_TYPE,
                                       "Положения заказчиков") if kind == "customer" else
                                      (settings.TYPAL_SUBSYSTEM, settings.TYPAL_DOCUMENT_TYPE, "Типовые положения"))
        progress.subject(f"{label}, регион {region}, {exact_date}")
        for attempt in range(settings.TYPAL_EIS_LIMIT_RETRIES + 1):
            await self._eis_pause()
            async with progress.step(f"запрос в ЕИС (getDocsByOrgRegionRequest, {subsystem})") as st:
                res = await self.eis._parse_eis_soap_ip("getDocsByOrgRegionRequest", subsystem_type=subsystem,
                                                        org_region=str(region), fz=223,
                                                        document_type=doc_type, exact_date=exact_date)
                st["detail"] = f"{res.get('status')}, файлов {len(res.get('files') or [])}"
            err = str(res.get("error") or "")
            if res.get("status") == "success" or not EIS_LIMIT_RE.search(err) or attempt == settings.TYPAL_EIS_LIMIT_RETRIES:
                break
            # «Код 13: превышено количество допустимых запросов … 90 за 60 сек» — лимит ЕИС общий для всех запросов
            # с адреса (в т. ч. ночной загрузки внешней системы): ждём окно и повторяем (22.10.2026: 15 из 54 запросов)
            logger.info(f"{label} {region} за {exact_date}: лимит запросов ЕИС — пауза "
                        f"{settings.TYPAL_EIS_LIMIT_WAIT_SEC} с, повтор")
            await asyncio.sleep(settings.TYPAL_EIS_LIMIT_WAIT_SEC)
        # итог запроса — для журнала дозагрузки (clause_backfill): «нет данных» не повторяется, сбой — повторяется
        self.last_eis = {"status": "success" if res.get("status") == "success" else
                         ("no_data" if NO_DATA_RE.search(err) else "error"), "error": err[:300] or None}
        if res.get("status") != "success":
            # «нет данных за дату» — обычный ответ ЕИС, а не сбой
            level = logger.info if NO_DATA_RE.search(err) else logger.warning
            level(f"{label} {region} за {exact_date}: {err[:300]}")
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
                logger.info(f"{f.get('filename')}: не положение о закупке ({e})")
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        return out

    # ------------------------------------------------------------------ разбор и сохранение
    async def load_xml(self, raw: bytes, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """Сохраняет версии положения из XML и читает его файлы.

        Args:
            raw: Байты XML ``typalOrderClause`` или ``orderClause``.
            source: Имя файла (для журнала).

        Returns:
            List[Dict[str, Any]]: ``registration_number``, ``version``, ``clause_type``, ``clause_id``, ``customers``,
            ``files``.

        Raises:
            ValueError: Это не ``typalOrderClause`` / ``orderClause``.
        """
        clauses = typal_clauses.parse_clause_xml(raw)
        self.stats["xml"] += 1
        out = []
        for c in clauses:
            ctype = c.get("clause_type") or "typal"
            ours = not (ctype == "customer" and self.only_inns
                        and not any(cu["inn"] in self.only_inns for cu in c["customers"]))
            if not ours and not self.keep_others_metadata:
                self.stats["skipped_not_monitored"] += 1
                continue
            label = "Положение заказчика" if ctype == "customer" else "Типовое положение"
            progress.subject(f"{label} {c['registration_number']} v{c['version']}")
            async with progress.step("сохранение версии, заказчиков и списка файлов") as st:
                cid = await self.typal.save_clause(c)
                st["detail"] = f"заказчиков {len(c['customers'])}, файлов {len(c['files'])}"
            self.stats["versions"] += 1
            files = await self._read_files(cid) if self.read_files and ours else []
            if not ours:
                self.stats["saved_metadata_only"] = self.stats.get("saved_metadata_only", 0) + 1
            logger.info(f"{label} {c['registration_number']} v{c['version']} ({source}): заказчиков "
                        f"{len(c['customers'])}, файлов {len(c['files'])}, прочитано {sum(1 for f in files if f['status'] == 'done')}")
            out.append({"registration_number": c["registration_number"], "version": c["version"], "clause_type": ctype,
                        "head_registration_number": c.get("head_registration_number"), "clause_id": cid,
                        "name": c.get("name"), "use_for_base_date": c.get("use_for_base_date"),
                        "customers": len(c["customers"]), "files": files})
        return out

    async def read_latest_files(self, inns: Optional[set] = None) -> Dict[str, int]:
        """Читает файлы только последних версий положений (после дозагрузки без файлов).

        Args:
            inns: Положения заказчиков — только с этими ИНН; типовые — все.

        Returns:
            Dict[str, int]: ``clauses`` — версий обработано, ``files`` — файлов прочитано.
        """
        rows = await self.repo.pool.fetch(
            """
            SELECT DISTINCT ON (c.registration_number) c.id, c.clause_type
            FROM rm_typal_clauses c
            WHERE c.clause_type = 'typal' OR $1::text[] IS NULL OR EXISTS (
                SELECT 1 FROM rm_typal_clause_customers cu WHERE cu.clause_id = c.id AND cu.inn = ANY($1::text[]))
            ORDER BY c.registration_number, c.version DESC
            """, sorted(inns) if inns else None)
        n_files = 0
        for r in rows:
            n_files += sum(1 for f in await self._read_files(int(r["id"])) if f["status"] == "done")
        return {"clauses": len(rows), "files": n_files}

    async def _read_files(self, clause_id: int) -> List[Dict[str, Any]]:
        """Скачивает и читает файлы версии без текста; положения режет по пунктам."""
        out = []
        for f in await self.typal.files_to_read(clause_id):
            try:
                async with progress.step(f"скачивание и чтение файла «{f['file_name']}» ({f['kind']})") as st:
                    doc_id, chunks = await self._read_one(f)
                    st["detail"] = f"документ {doc_id}, пунктов {chunks}" if doc_id else "текст не извлечён"
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

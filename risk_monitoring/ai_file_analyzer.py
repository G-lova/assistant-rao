"""Анализ файлов риск-мониторинга (``risk_monitoring_files``) → AI-паспорт документа во внешней БД.

Конвейер одного файла (концепция, гл. 18.2):

1. скачивание → SHA-256 → документ в ``ai_documents`` (дедупликация одинаковых файлов разных закупок);
2. текст (``FileReader``, OCR при необходимости) — один раз на содержимое;
3. чанки и эмбеддинги в pgvector → похожие документы (база знаний документов, гл. 18.13);
   + контроль качества текста (``text_quality``): циклы повторов OCR схлопываются, негодный текст
   не анализируется (``pipeline.result = ocr_bad``);
4. извлечение данных с доказательной базой (``DataExtractor``: организации, суммы, даты, нормы, ТРУ …);
   реквизиты (ИНН, КПП, ОГРН, номера), которых нет в тексте, снимаются;
5. тип документа (``doc_kind``): печатная форма ЕИС — по тегу XML-источника; иначе ``TypeDetector``
   с подсказкой по имени файла (при неуверенном детекторе выигрывает имя);
6. AI-паспорт: заключение, назначение, структура, ключевые условия, тематика, технические объекты,
   итоговая оценка и рекомендации (``DocumentProfiler``);
7. признаки рисков по тексту с проверкой цитат (``DocumentRiskAnalyzer``) — только коды, допустимые
   для типа документа (``doc_kind.allowed_codes``);
8. сравнение с предыдущей редакцией: текстовый и смысловой diff, риски изменений, сопоставление рисков
   (что сохранилось, что появилось, что не выявлено в новой версии);
9. запись ``ai_analysis`` через ``PATCH /api/risk-monitoring/ai-analysis``.

Каждый шаг LLM кэшируется в ``ai_document_analysis`` по (документ, тип анализа, версия промпта, модель).
Распознавание и извлечение выполняет модель ``get_llm()``; паспорт, риски и сравнение редакций —
модель ``RM_REASONING_MODEL`` (см. ``llm_json.reasoning_llm``).

**Повторного анализа нет.** Файл, у которого во внешней БД уже ``pipeline.status = completed``,
пропускается. Повторно анализируются только новые версии документов (это новые строки файлов).
Перезапуск уже проанализированного файла — только в режиме отладки (``force`` при
``RISK_MONITORING_DEBUG=true``); ``recompute`` дополнительно игнорирует кэш LLM.
"""
import asyncio
import hashlib
import json
import os
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from configs.config import Config
from configs.logger import get_logger
from risk_monitoring import (boilerplate, doc_kind, file_versions, norms, passport, pdf_text, printform_passport,
                             risk_catalog, rm_sql, settings, text_quality, version_diff, xml_events)
from risk_monitoring import progress
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

OCR_ERROR_RE = re.compile(r"Ошибка при анализе изображения|Таймаут при анализе изображения|\"status\":\s*\"error\"")
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


def _card(value: Any) -> Optional[int]:
    """id карточки закупки из строки внешней БД (``None``, NaN и пустые значения — ``None``)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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

        from risk_monitoring.llm_json import reasoning_llm

        client, self.extraction_model = get_llm()
        r_client, self.model = reasoning_llm(client, self.extraction_model)
        self.parser = CloudStorageParser(http_manager, None)
        # Компоненты экспертизы сами повторяют запрос (async_retry: 2→4→8→16 с); встроенные повторы SDK
        # (ещё 2 через ~1 с) на 504 шлюза только умножают нагрузку — для риск-мониторинга отключены.
        from configs.llm_resilience import ResilientClient

        ex_client = ResilientClient(client, retries=settings.LLM_PROXY_RETRIES)
        self.file_reader = FileReader(ex_client, self.extraction_model)
        self.type_detector = TypeDetector(ex_client, self.extraction_model)
        self.extractor = DataExtractor(ex_client, self.extraction_model)
        self.profiler = DocumentProfiler(r_client, self.model)
        self.risk_analyzer = DocumentRiskAnalyzer(r_client, self.model)
        self.differ = version_diff.DocumentDiffAnalyzer(r_client, self.model)
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
        # Сколько анализов выполнено заново и сколько взято из прошлой редакции (сводка задачи)
        self.stats: Dict[str, Dict[str, int]] = {"produced": {}, "reused_previous": {}, "modes": {}}

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

    async def _query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Выполняет запрос ``queries/rm/<name>`` и возвращает строки как словари."""
        sql, bindings = rm_sql.render(name, head=head, **lists)
        df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [{k: _none(v) for k, v in r.items()} for r in df.to_dict("records")] if not df.empty else []

    def _with_url(self, r: Dict[str, Any]) -> Dict[str, Any]:
        """Добавляет ссылку для скачивания: хранилище внешней системы (``path``) или ЕИС (``source_url``)."""
        r["url"] = f"{self.storage_path}{r['path']}" if r.get("path") else r.get("source_url")
        return r

    async def fetch_previous_versions(self, rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Предыдущие версии файлов через diff вложений XML (``file_versions``).

        1. XML файла — ``xml_source_id`` или XML с тем же ``owner_number``, версией и группой, в чьих
           вложениях есть ссылка файла.
        2. Предыдущая версия XML — ``xml_previous.sql``.
        3. Diff вложений: заменённый файл → его прежняя ссылка; печатная форма → печатная форма прошлого XML;
           добавленный файл → предыдущей версии нет.
        4. Если XML не прочитаны — запасной поиск по имени файла (``files_previous.sql``).

        Args:
            rows: Строки ``files_meta.sql``.

        Returns:
            Dict[int, Dict[str, Any]]: ``id файла → строка предыдущей версии`` с ``url``, ``match``,
            ``previous_xml_id``.
        """
        from risk_monitoring.xml_event_analyzer import ArchiveCache

        # Версия файла у загрузчика может быть неточной, поэтому XML читаются и для eis_version = 0
        # (у первой версии предыдущей нет, но может быть следующая — superseded_by)
        if not rows:
            return {}
        result: Dict[int, Dict[str, Any]] = {}
        resolved: set = set()
        if not hasattr(self, "lookups"):
            self.lookups: Dict[int, Dict[str, Any]] = {}
        archives = ArchiveCache(self.parser)
        try:
            xml_of = await self._current_xml(rows, archives)
            xml_ids = sorted({x["id"] for x in xml_of.values()})
            prev_rows = await self._query("xml_previous.sql", head=[self.storage_path], ids=xml_ids) if xml_ids else []
            prev_xml: Dict[int, Dict[str, Any]] = {}
            for p in prev_rows:
                cur = int(p["current_id"])
                if cur not in prev_xml or (p.get("eis_version") or -1) > (prev_xml[cur].get("eis_version") or -1):
                    prev_xml[cur] = p
            refs: Dict[int, Dict[str, Any]] = {}
            for r in rows:
                fid = int(r["id"])
                x = xml_of.get(fid)
                if not x:
                    self.lookups[fid] = {"match": "xml_not_found", "file_eis_version": r.get("eis_version"),
                                         "note": "XML документа не найден ни по xml_source_id, ни по owner_number "
                                                 "(среди вложений и печатных форм XML нет ссылки файла)"}
                    continue
                px = prev_xml.get(int(x["id"]))
                try:
                    body = x.get("body") or (await self._xml_body(archives, x))
                    prev_body = await self._xml_body(archives, px) if px else None
                except Exception as e:  # noqa: BLE001 — запасной путь по имени файла
                    logger.warning(f"Файл {fid}: XML {x['id']} / предыдущий не прочитан: {e}")
                    continue
                ref = file_versions.previous_ref(r, body, prev_body)
                ref["previous_xml_id"] = int(px["id"]) if px else None
                ref["xml_id"], ref["xml_eis_version"] = int(x["id"]), x.get("eis_version")
                for nx in x.get("newer") or []:
                    try:
                        nbody = await self._xml_body(archives, nx)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"Файл {fid}: новая версия XML {nx['id']} не прочитана: {e}")
                        continue
                    nxt = file_versions.next_ref(r, body, nbody)
                    if nxt:
                        ref["superseded"] = {**nxt, "xml_id": int(nx["id"]), "xml_eis_version": nx.get("eis_version")}
                        break
                refs[fid] = ref
                resolved.add(fid)
            urls = sorted({u for ref in refs.values() for u in (ref.get("urls") or [])
                           + ([ref["superseded"]["url"]] if (ref.get("superseded") or {}).get("url") else [])})[:500]
            pids = sorted({ref["previous_xml_id"] for ref in refs.values() if ref.get("previous_xml_id")})
            cands = await self._query("files_by_ref.sql", urls=urls, ids=pids) if (urls or pids) else []
            for r in rows:
                fid = int(r["id"])
                ref = refs.get(fid)
                if not ref:
                    continue
                prev = file_versions.pick_previous(ref, r, cands, ref.get("previous_xml_id"))
                self.lookups[fid] = file_versions.lookup_summary(ref, prev, cands, r)
                if prev:
                    result[fid] = self._with_url({**prev, "match": ref["match"],
                                                  "previous_xml_id": ref.get("previous_xml_id")})
                logger.info(f"Файл {fid}: предыдущая версия — {prev and prev['id']} ({ref['match']})")
        except Exception as e:  # noqa: BLE001 — без предыдущей версии анализ всё равно возможен
            logger.warning(f"Предыдущие версии файлов через XML не найдены: {type(e).__name__}: {e}")
        finally:
            archives.close()

        rest = [int(r["id"]) for r in rows if int(r["id"]) not in resolved]
        if rest:
            try:
                for p in await self._query("files_previous.sql", ids=rest):
                    cur = int(p["current_id"])
                    if cur not in result or (p.get("eis_version") or 0) > (result[cur].get("eis_version") or 0):
                        result[cur] = self._with_url({**p, "match": "file_name"})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Предыдущие версии файлов (по имени) не получены: {e}")
        return result

    async def _read_xml(self, archives: Any, x: Dict[str, Any]) -> Dict[str, Any]:
        """Читает XML из архива и возвращает нормализованное тело."""
        _, _, body = xml_events.parse_xml(await archives.read(x["archive_url"], x["entry_name"]))
        return body

    async def _current_xml(self, rows: List[Dict[str, Any]], archives: Any) -> Dict[int, Dict[str, Any]]:
        """XML каждого файла: по ``xml_source_id``, иначе по ``owner_number`` с проверкой вложений.

        Args:
            rows: Файлы.
            archives: ``ArchiveCache``.

        Returns:
            Dict[int, Dict[str, Any]]: ``id файла → строка XML`` (``id``, ``archive_url``, ``entry_name``,
            для найденных по номеру — ``body``).
        """
        out: Dict[int, Dict[str, Any]] = {}
        linked = sorted({int(r["xml_source_id"]) for r in rows if r.get("xml_source_id")})
        if linked:
            meta = {int(x["id"]): x for x in await self._query("xml_meta.sql", head=[self.storage_path], ids=linked)}
            for r in rows:
                if r.get("xml_source_id") and int(r["xml_source_id"]) in meta:
                    out[int(r["id"])] = meta[int(r["xml_source_id"])]
        nums = sorted({str(r["owner_number"]) for r in rows if r.get("owner_number")})
        cands = await self._query("xml_by_owner.sql", head=[self.storage_path], nums=nums, nums2=nums) if nums else []
        for r in rows:
            if int(r["id"]) in out or not r.get("owner_number"):
                continue
            own = [c for c in cands if str(r["owner_number"]) in (str(c.get("purchase_number")), str(c.get("contract_number")))]
            # Версия файла у загрузчика может не совпадать с версией XML, к которому приложен файл
            # (вложения прошлой редакции извещения приходят с eis_version новой) — проверяем все версии.
            for c in file_versions.choose_xml({**r, "eis_version": None}, own)[:8]:
                try:
                    body = await self._xml_body(archives, c)
                except Exception as e:  # noqa: BLE001 — пробуем следующий кандидат
                    logger.warning(f"Файл {r['id']}: XML {c['id']} не прочитан: {e}")
                    continue
                if file_versions.contains(body, r):
                    out[int(r["id"])] = {**c, "body": body}
                    break
        for r in rows:
            x = out.get(int(r["id"]))
            if x is not None:
                x = out[int(r["id"])] = dict(x)
                x["newer"] = file_versions.newer_versions(x, cands)
        return out

    async def _xml_body(self, archives: Any, x: Dict[str, Any]) -> Dict[str, Any]:
        """Тело XML с кэшем на время запуска (одни и те же XML нужны нескольким файлам)."""
        cache = self.__dict__.setdefault("_xml_cache", {})
        key = int(x["id"])
        if key not in cache:
            cache[key] = await self._read_xml(archives, x)
        return cache[key]

    async def analyze_ids(self, file_ids: List[int]) -> List[Dict[str, Any]]:
        """Анализирует файлы по id.

        Args:
            file_ids: id файлов ``risk_monitoring_files``.

        Returns:
            List[Dict[str, Any]]: По одному результату на id: ``status`` (``done`` / ``skipped`` /
            ``not_found`` / ``error``), ``pushed``, ``ai_analysis`` (кроме пропущенных).
        """
        ids = list(dict.fromkeys(int(i) for i in file_ids))
        progress.subject(f"Файлы ({len(ids)})")
        async with progress.step("чтение сведений о файлах из внешней БД") as st:
            rows = await self.fetch_files_meta(ids)
            st["detail"] = f"найдено {len(rows)} из {len(ids)}"
        found = {int(r["id"]): r for r in rows}
        todo = [r for r in found.values() if self.force or r.get("rm_status") != "completed"]
        async with progress.step("поиск прошлых редакций файлов (XML ЕИС)") as st:
            previous = await self.fetch_previous_versions(todo) if todo else {}
            st["detail"] = f"к анализу {len(todo)}, с прошлой редакцией {len(previous)}"
        async with progress.step("чтение результатов из внутренней БД"):
            self.internal = await self._internal(list(found) + [int(p["id"]) for p in previous.values() if p.get("id")])
        by_id: Dict[int, Dict[str, Any]] = {}
        for wave in self._waves([i for i in ids if i in found], previous, {r["id"] for r in todo}):
            for res in await asyncio.gather(*(self._guarded(found[i], previous.get(i)) for i in wave)):
                by_id[int(res.get("file_id"))] = res
        results = [by_id[i] for i in ids if i in by_id]
        missing = [{"file_id": i, "status": "not_found"} for i in ids if i not in found]
        return list(results) + missing

    @staticmethod
    def _waves(ids: List[int], previous: Dict[int, Dict[str, Any]], todo: Iterable[Any]) -> List[List[int]]:
        """Порядок анализа: прошлая редакция раньше новой, если обе в одном запуске.

        Новая редакция переиспользует анализ прошлой (данные, паспорт, перенос рисков) и сравнивает риски
        (``risks_comparison``) — для этого прошлая должна быть проанализирована первой. Файлы без связи
        идут в первой волне параллельно; цепочка v0 → v1 → v2 — тремя волнами.

        Args:
            ids: id файлов в порядке запроса.
            previous: ``id → предыдущая версия``.
            todo: id файлов, которые будут анализироваться (не пропущены как уже проанализированные).

        Returns:
            List[List[int]]: Волны id.
        """
        todo_set = {int(t) for t in todo}
        depth: Dict[int, int] = {}

        def level(i: int, seen: frozenset = frozenset()) -> int:
            """Глубина файла в цепочке редакций внутри запуска."""
            if i in depth:
                return depth[i]
            prev = previous.get(i)
            p = int(prev["id"]) if prev and prev.get("id") is not None else None
            depth[i] = level(p, seen | {i}) + 1 if p is not None and p in todo_set and p in ids and p not in seen else 0
            return depth[i]

        for i in ids:
            level(i)
        waves: Dict[int, List[int]] = {}
        for i in ids:
            waves.setdefault(depth[i], []).append(i)
        return [waves[k] for k in sorted(waves)]

    async def _guarded(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Обёртка одного файла: семафор, правило «без повторного анализа», обработка ошибок."""
        file_id = int(row["id"])
        attempt = int(row.get("rm_attempt") or 0) + 1
        progress.subject(f"Файл {file_id} «{row.get('file_name') or ''}»")
        if not self.force:
            if row.get("rm_status") == "completed":
                progress.note("уже проанализирован во внешней БД — пропуск")
                return {"file_id": file_id, "status": "skipped", "reason": "already_analyzed"}
            stored = (getattr(self, "internal", None) or {}).get(file_id)
            if stored and stored.get("status") == "completed" and not stored.get("debug"):
                return await self._reuse_internal(file_id, stored)
            if row.get("rm_status") == "failed" and attempt > MAX_ATTEMPTS:
                progress.note(f"исчерпаны попытки ({MAX_ATTEMPTS}) — пропуск")
                return {"file_id": file_id, "status": "skipped", "reason": "max_attempts"}
        async with self.semaphore:
            progress.note(f"начало анализа (попытка {attempt})")
            try:
                return await self.analyze_file(row, previous, attempt)
            except FileUnavailable as e:
                logger.warning(f"Файл {file_id} недоступен: {e}")
                return await self._push_failure(file_id, attempt, f"download_failed: {e}")
            except Exception as e:  # noqa: BLE001 — сбой одного файла не останавливает остальные
                logger.error(f"Ошибка анализа файла {file_id}: {e}", exc_info=True)
                return await self._push_failure(file_id, attempt, f"{type(e).__name__}: {e}")

    async def _reuse_internal(self, file_id: int, stored: Dict[str, Any]) -> Dict[str, Any]:
        """Файл уже проанализирован, но результата нет во внешней БД (запись не удалась или прогон был без записи):
        дозаписываем сохранённый результат, без повторного анализа.

        Returns:
            Dict[str, Any]: ``status = done``, ``source = internal``.
        """
        ai = stored["ai_analysis"]
        result = {"file_id": file_id, "document_id": None, "status": "done", "source": "internal",
                  "pushed": False, "ai_analysis": ai}
        progress.note(f"результат есть во внутренней БД (анализ {stored.get('analyzed_at')}) — без повторного анализа")
        if self.push_results:
            try:
                async with progress.step("дозапись результата во внешнюю БД"):
                    await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, file_id, ai)
                result["pushed"] = True
                if hasattr(getattr(self, "repo", None), "mark_rm_pushed"):
                    await self.repo.mark_rm_pushed(self.env, "file", file_id)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось дозаписать ai_analysis файла {file_id}: {e}")
                result["push_error"] = str(e)
        logger.info(f"Файл {file_id}: результат из внутренней БД (анализ {stored.get('analyzed_at')}), "
                    f"записан во внешнюю БД: {result['pushed']}")
        return result

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
        superseded = self._superseded_analysis(row, previous, attempt)
        if superseded is not None:
            progress.note("прошлая редакция, уже заменённая новой — паспорт без анализа")
            return await self._finalize(file_id, None, superseded)
        if not row.get("url"):
            raise FileUnavailable("нет пути к файлу и ссылки ЕИС")
        purchase = purchase_context(row)
        prev_id = int(previous["id"]) if previous else None

        async with progress.step("скачивание файла и чтение текста") as st:
            document_id, text, sha, size = await self._load_document(file_id, row["url"], file_name,
                                                                     card_id=row.get("risk_monitoring_contract_id"))
            st["detail"] = f"документ {document_id}, {len(text or '')} символов" if text else "текст не извлечён"
        if text is None:
            pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions, prev_id,
                                               self.force, attempt, result="no_text")
            ai = passport.no_text_analysis(row, "Не удалось извлечь текст документа", pipeline, self.model, sha)
            return await self._finalize(file_id, document_id, ai)

        text = text_quality.unwrap_ocr_json(text)
        quality = text_quality.assess(text)
        print_kind = doc_kind.print_form_kind(row) if doc_kind.is_print_form(row) else None
        if quality["status"] == "bad":
            return await self._finalize(file_id, document_id, self._bad_text_analysis(
                row, quality, print_kind, prev_id, attempt, sha))
        text = quality["text"]
        progress.note(f"качество текста: {quality['status']}" + (f", печатная форма ЕИС ({print_kind.get('detected_type')})" if print_kind else ""))

        from configs.utils import split_large_text
        from knowledge_store.pages import PAGE_MARKER_RE

        async with progress.step("эмбеддинги документа") as st:
            embeddings_ok = await self._ensure_embeddings(document_id, text)
            st["detail"] = "готово" if embeddings_ok else "не получены"
        similar = []
        if embeddings_ok:
            async with progress.step("поиск похожих документов") as st:
                similar = await self._find_similar(document_id)
                st["detail"] = f"найдено {len(similar)}"

        fz = risk_catalog.normalize_fz(row.get("fz_type"))
        law = f"{fz}-ФЗ" if fz else "закон не определён"

        # Прошлая редакция: текст и diff нужны до анализа — от них зависит режим
        prev_ctx = None
        if previous:
            async with progress.step(f"загрузка прошлой редакции (файл {previous.get('id')})"):
                prev_ctx = await self._load_previous(previous, sha, file_name)
        diff = None
        if prev_ctx and prev_ctx.get("text") is not None:
            diff = version_diff.text_diff(prev_ctx["text"], text)

        if print_kind:
            mode = "print_form"
            extraction, profile, candidates = printform_passport.build(text, print_kind, row)
            type_info = {"detected_type": print_kind["detected_type"], "confidence": 1.0, "issues": [],
                         "key_indicators": [], "type_source": "print_form", "print_form": print_kind}
            type_decode = print_kind["type_decode"]
            allowed = doc_kind.allowed_codes(type_info["detected_type"], print_kind["group"])
            summary = extraction["raw_data"].get("summary")
            risk = None
            if candidates and allowed:
                brand_text = "\n".join(candidates)
                ctx = {"detected_type": type_info["detected_type"], "type_decode": type_decode, "summary": summary,
                       "purchase": purchase, "law": law, "norms": norms.digest("docOpusObjectZacupFiles", fz),
                       "note": "Переданы только строки описания объекта закупки с признаками товарного знака"}
                risk = await self._cached(document_id, "risk_analysis_print_form",
                                          lambda: self.risk_analyzer.analyze(brand_text, file_name, ctx, codes=allowed),
                                          compact=self.risk_analyzer.to_compact, keep=lambda r: not r.get("partial"),
                                          version=f"{self.versions['risk_analysis']}:{_hash_version(allowed, candidates)}",
                                          model=self.model)
            if risk is None:
                risk = self.risk_analyzer.merge_results([], None, allowed)
                risk["summary"] = ("Товарных знаков в описании объекта закупки не найдено." if allowed
                                   else "Для документа этого типа поиск рисков в тексте не проводится.")
            removed_ids = []
            version_changes, version_risks = (await self._version_changes(previous, prev_ctx, diff, document_id,
                                                                          file_name, type_decode, use_llm=False)
                                              if previous else (None, []))
        else:
            reuse_doc = None
            if diff and prev_ctx.get("doc_id") and (diff["identical"] or diff["similarity"] >= version_diff.INCREMENTAL_SIMILARITY):
                reuse_doc = prev_ctx["doc_id"]
            chunks = split_large_text(text, max_chunk_size=LLM_CHUNK_TOKENS) or [text]
            extraction = await self._cached(document_id, "data_extraction",
                                            lambda: self.extractor.extract_data_from_document(chunks, file_name),
                                            keep=lambda r: isinstance(r, dict) and bool(r.get("raw_data")),
                                            fallback_doc=reuse_doc)
            if isinstance(extraction, str):
                extraction = json.loads(extraction)
            extraction = dict(extraction or {})
            raw_data, removed_ids = text_quality.verify_identifiers(extraction.get("raw_data") or {}, text)
            if reuse_doc:
                raw_data, stale = version_diff.drop_stale_evidence(raw_data, text)
                removed_ids += [f"сведение прошлой редакции, которого нет в новой: {x}" for x in stale]
            extraction["raw_data"] = text_quality.clean_value(raw_data)
            type_info, type_decode = await self._doc_type(document_id, chunks, file_name, extraction, row, None,
                                                          fallback_doc=reuse_doc)
            summary = (extraction.get("raw_data") or {}).get("summary")
            allowed = [] if doc_kind.no_risk_by_name(file_name) else doc_kind.allowed_codes(type_info["detected_type"], None)
            profile = await self._cached(document_id, "document_profile",
                                         lambda: self.profiler.profile(text, file_name, type_decode, summary, purchase),
                                         model=self.model, fallback_doc=reuse_doc)
            risk_context = {"detected_type": type_info.get("detected_type"), "type_decode": type_decode,
                            "summary": summary, "purchase": purchase, "law": law,
                            "norms": norms.digest(type_info.get("detected_type"), fz)}
            risk_version = (f"{self.versions['risk_analysis']}:{_hash_version(allowed)}:"
                            f"{norms.version(type_info.get('detected_type'), fz)}")
            self._risk_version = risk_version
            prev_risk = (await self._get_cached(reuse_doc, "risk_analysis", risk_version, self.model)) if reuse_doc else None
            if prev_risk is not None:
                mode = "incremental"
                risk = await self._cached(
                    document_id, "risk_analysis",
                    lambda: self._incremental_risks(prev_risk, prev_ctx["text"], text, file_name, risk_context, allowed),
                    compact=self.risk_analyzer.to_compact, keep=lambda r: not r.get("partial"),
                    version=risk_version, model=self.model)
            else:
                mode = "full"
                risk = await self._cached(document_id, "risk_analysis",
                                          lambda: self.risk_analyzer.analyze(text, file_name, risk_context, codes=allowed),
                                          compact=self.risk_analyzer.to_compact,
                                          keep=lambda r: not r.get("partial"),
                                          version=risk_version, model=self.model)
            version_changes, version_risks = (await self._version_changes(previous, prev_ctx, diff, document_id,
                                                                          file_name, type_decode, use_llm=True)
                                              if previous else (None, []))
        async with progress.step("фильтр типовых формулировок") as st:
            risk, typical = await self._drop_typical(risk, document_id, row)
            st["detail"] = f"снято {len(typical)}" if typical else "типовых не найдено"
        risk_compact = self.risk_analyzer.to_compact(risk) if risk else {}

        prev_codes = await self._previous_codes(previous, prev_ctx, allowed) if previous else None
        pages = len({m.group(1) for m in PAGE_MARKER_RE.finditer(text)}) or None
        ocr_used = "(OCR)" in text
        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions, prev_id, self.force, attempt,
                                           result="ocr_suspect" if quality["status"] == "suspect" else None)
        pipeline["models"] = {"extraction": self.extraction_model, "reasoning": self.model}
        pipeline["mode"] = mode
        if getattr(self, "stats", None) is not None:
            self.stats["modes"][mode] = self.stats["modes"].get(mode, 0) + 1
        if mode == "incremental":
            pipeline["incremental"] = {k: (risk or {}).get(k) for k in ("carried_over", "found_in_changes", "changed_share")}
        ai = passport.build_file_analysis(
            file_meta=row, type_info=type_info, type_decode=type_decode, extraction=extraction, profile=profile,
            risk_compact=risk_compact, extra_risks=version_risks, similar=similar, version_changes=version_changes,
            previous_codes=prev_codes, pages=pages,
            text_chars=len(text), ocr_used=ocr_used, purchase=purchase, pipeline=pipeline, model=self.model,
            content_sha256=sha,
        )
        self._annotate(ai, quality, removed_ids, allowed, (risk or {}).get("rejected_inconsistent"))
        if typical:
            ai["risk_scope"]["typical_dropped"] = typical
        self._add_lookup(ai, file_id)
        progress.note(f"анализ готов: режим {mode}, тип {type_info.get('detected_type')}, рисков {len(ai.get('risks') or [])}")
        return await self._finalize(file_id, document_id, ai)

    async def _drop_typical(self, risk: Optional[Dict[str, Any]], document_id: int,
                            row: Dict[str, Any]) -> tuple:
        """Снимает риски, цитата которых — типовая формулировка многих заказчиков (``boilerplate``).

        Args:
            risk: Результат риск-анализа (формат ``DocumentRiskAnalyzer.analyze``).
            document_id: ``ai_documents.id``.
            row: Строка ``files_meta.sql`` (карточка и заказчик).

        Returns:
            tuple: ``(риск-анализ без типовых рисков, [{code, fragment, sources}])``. Сбой поиска не ломает анализ.
        """
        if not risk or not risk.get("risks") or not settings.BOILERPLATE_ENABLED:
            return risk, []
        frags = [((r.get("evidence") or {}).get("fragment") or "") for r in risk["risks"]]

        async def fetch_orgs(cards: List[int]) -> Dict[int, Any]:
            """Заказчики карточек из внешней БД."""
            rows = await self._query("card_orgs.sql", ids=cards)
            return {int(r["id"]): r.get("org_id") for r in rows}

        try:
            counts = await boilerplate.typical_fragments(
                frags, embedder=self.embedder, repo=self.repo, env=self.env, document_id=document_id,
                own_card=row.get("risk_monitoring_contract_id"), own_org=row.get("organisation_id"),
                embedding_model=self.embedding_model, fetch_orgs=fetch_orgs, own_org_name=row.get("customer_name"))
        except Exception as e:  # noqa: BLE001 — без фильтра риски остаются
            logger.warning(f"Проверка типовых формулировок не выполнена (документ {document_id}): {type(e).__name__}: {e}")
            return risk, []
        need = boilerplate.min_sources()
        kept, dropped = [], []
        for r, frag in zip(risk["risks"], frags):
            n = counts.get(frag, 0)
            if n >= need:
                dropped.append({"code": r.get("code"), "fragment": frag[:200], "sources": n})
            else:
                kept.append(r)
        if not dropped:
            return risk, []
        logger.info(f"Документ {document_id}: сняты типовые формулировки — {[d['code'] for d in dropped]}")
        new = dict(risk)
        new["risks"] = kept
        new["risk_score"] = self.risk_analyzer._score(kept)
        new["risk_level"] = self.risk_analyzer._level(new["risk_score"])
        return new, dropped

    async def _previous_codes(self, previous: Dict[str, Any], prev_ctx: Optional[Dict[str, Any]],
                              allowed: List[str]) -> Optional[List[str]]:
        """Коды рисков прошлой редакции для ``risks_comparison``.

        Источники по порядку: ``ai_analysis`` прошлой редакции во внешней БД (если она проанализирована без
        рисков, ``JSON_EXTRACT`` даёт ``NULL`` — это пустой список, а не «нет данных»); сохранённый у нас результат
        (``ai_rm_results``); риск-анализ прошлой редакции из кэша сервиса.

        Returns:
            Optional[List[str]]: Коды или ``None`` (прошлая редакция не анализировалась).
        """
        codes = _json_list(previous.get("rule_ids"))
        if codes is not None:
            return codes
        if previous.get("rm_status") == "completed":
            return []
        prev_id = int(previous["id"]) if previous.get("id") else None
        stored = (getattr(self, "internal", None) or {}).get(prev_id) if prev_id else None
        if stored is None and prev_id:
            stored = (await self._internal([prev_id])).get(prev_id)
        if stored and stored.get("status") == "completed":
            # прошлая редакция проанализирована, но во внешней БД результата нет (без записи / сбой записи)
            return sorted({r.get("rule_id") for r in (stored["ai_analysis"].get("risks") or []) if r.get("rule_id")})
        doc_id = (prev_ctx or {}).get("doc_id")
        version = getattr(self, "_risk_version", None) or f"{self.versions['risk_analysis']}:{_hash_version(allowed)}"
        cached = await self._get_cached(doc_id, "risk_analysis", version, self.model) if doc_id else None
        if cached is not None:
            return sorted({r.get("code") for r in cached.get("risks") or [] if r.get("code")})
        return None

    def _superseded_analysis(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]],
                             attempt: int) -> Optional[Dict[str, Any]]:
        """Паспорт без анализа для редакции, уже заменённой новой (``superseded_by``).

        Анализируются только актуальные редакции: файл прошлой редакции, пришедший в той же выгрузке, что и
        новая, получает паспорт с отметкой о замене, без скачивания и без запросов к модели. В режиме
        отладки (``force``) или при ``settings.ANALYZE_SUPERSEDED`` файл анализируется как обычно.

        Returns:
            Optional[Dict[str, Any]]: ``ai_analysis`` (``pipeline.result = superseded``) или ``None``.
        """
        lk = (getattr(self, "lookups", None) or {}).get(int(row["id"])) or {}
        sup = lk.get("superseded_by") or {}
        if not sup.get("file_id") or self.force or settings.ANALYZE_SUPERSEDED:
            return None
        from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING

        reason = (f"Редакция заменена: в версии XML {sup.get('xml_eis_version')} вместо этого файла приложен "
                  f"файл {sup['file_id']} («{sup.get('file_name')}»). Анализируется актуальная редакция.")
        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions,
                                           int(previous["id"]) if previous else None, self.force, attempt,
                                           result="superseded")
        ai = passport.no_text_analysis(row, reason, pipeline, None, None)
        ai["readability"] = {"status": None, "issues": [], "readability_score": None, "pages": None, "text_chars": None}
        by_name = doc_kind.type_by_name(row.get("file_name"))
        if by_name:
            ai["doc_type"] = {"detected_type": by_name, "type_decode": DOCUMENT_TYPE_MAPPING.get(by_name),
                              "confidence": None, "issues": [], "type_source": "file_name"}
        ai["superseded_by"] = sup
        ai["version_lookup"] = lk
        return ai

    def _add_lookup(self, ai: Dict[str, Any], file_id: int) -> None:
        """Дописывает в ``ai_analysis`` ход поиска версий (``version_lookup``) и ``superseded_by``.

        ``superseded_by`` — файл новой редакции, если этот файл в следующей версии XML заменён: паспорт такого
        файла описывает уже неактуальную редакцию.
        """
        lk = (getattr(self, "lookups", None) or {}).get(int(file_id))
        if not lk:
            return
        ai["version_lookup"] = lk
        if lk.get("superseded_by"):
            ai["superseded_by"] = lk["superseded_by"]

    async def _read_text(self, path: str, file_name: str, ext: str) -> Optional[str]:
        """Текст файла: для PDF — текстовый слой и OCR только страниц-сканов (``pdf_text``), иначе ``FileReader``.

        Args:
            path: Путь к скачанному файлу.
            file_name: Имя файла.
            ext: Расширение.

        Returns:
            Optional[str]: Текст.
        """
        if ext == ".pdf":
            try:
                res = await pdf_text.read_pdf(path, file_name, getattr(self.file_reader, "ocr_processor", None))
                if res and res["text"].strip():
                    return res["text"]
            except Exception as e:  # noqa: BLE001 — переходим на общий FileReader
                logger.warning(f"pdf_text: {file_name}: {type(e).__name__}: {e}; читаю через FileReader")
        return await self.file_reader.read_file(path, file_name)

    def _bad_text_analysis(self, row: Dict[str, Any], quality: Dict[str, Any], print_kind: Optional[Dict[str, Any]],
                           prev_id: Optional[int], attempt: int, sha: str) -> Dict[str, Any]:
        """``ai_analysis`` для негодного текста (сбой OCR): без запросов к модели и без рисков.

        Тип документа берётся из печатной формы или имени файла.

        Args:
            row: Строка ``files_meta.sql``.
            quality: Результат ``text_quality.assess``.
            print_kind: Вид печатной формы или ``None``.
            prev_id: id предыдущей версии файла.
            attempt: Номер попытки.
            sha: SHA-256 содержимого.

        Returns:
            Dict[str, Any]: ``ai_analysis`` (``pipeline.result = ocr_bad``).
        """
        from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING

        reason = "Текст документа распознан с ошибками, анализ ИИ не проводился: " + "; ".join(quality["issues"])
        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions, prev_id,
                                           self.force, attempt, result="ocr_bad")
        pipeline["models"] = {"extraction": self.extraction_model, "reasoning": self.model}
        ai = passport.no_text_analysis(row, reason, pipeline, self.model, sha)
        if print_kind:
            ai["doc_type"] = {"detected_type": print_kind["detected_type"], "type_decode": print_kind["type_decode"],
                              "confidence": 1.0, "issues": [], "type_source": "print_form", "print_form": print_kind}
        else:
            by_name = doc_kind.type_by_name(row.get("file_name"))
            if by_name:
                ai["doc_type"] = {"detected_type": by_name, "type_decode": DOCUMENT_TYPE_MAPPING.get(by_name),
                                  "confidence": None, "issues": [], "type_source": "file_name"}
        ai["readability"]["text_quality"] = {k: quality[k] for k in ("status", "loops", "loop_share", "letters")}
        return ai

    @staticmethod
    def _annotate(ai: Dict[str, Any], quality: Dict[str, Any], removed_ids: List[str], allowed: List[str],
                  rejected: Optional[int]) -> None:
        """Дописывает в ``ai_analysis`` результаты контроля качества: текст, реквизиты, отбор рисков.

        Args:
            ai: ``ai_analysis`` файла (меняется на месте).
            quality: Результат ``text_quality.assess``.
            removed_ids: Снятые реквизиты, которых нет в тексте.
            allowed: Коды рисков, которые искались в документе.
            rejected: Сколько рисков модели отброшено как противоречивые.
        """
        rd = ai.setdefault("readability", {})
        noise = "не предоставила явную оценку"
        rd["issues"] = [i for i in (rd.get("issues") or []) if noise not in str(i)] + quality["issues"]
        rd["text_quality"] = {k: quality[k] for k in ("status", "loops", "loop_share", "letters")}
        if removed_ids:
            ai["raw_data_issues"] = [f"Снят реквизит, которого нет в тексте: {x}" for x in removed_ids]
        ai["risk_scope"] = {"allowed_codes": allowed, "rejected_inconsistent": rejected or 0}

    async def _load_document(self, file_id: int, url: str, file_name: str, card_id: Optional[int] = None):
        """Скачивает файл, находит или создаёт документ по хэшу содержимого и связывает его с файлом.

        Args:
            file_id: id внешнего файла.
            url: Ссылка на файл (хранилище внешней системы или ЕИС).
            file_name: Имя файла.
            card_id: ``risk_monitoring_contracts.id`` — карточка закупки (срок хранения ссылки).

        Текст с признаками сбоя OCR (циклы повторов) сохраняется со статусом ``suspect``: при обычном
        прогоне он берётся из кэша, при отладочном (``force``) файл читается заново.

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
            progress.note(f"файл скачан: {size / 1024:.0f} КБ, {ext}")
            doc = await self.repo.get_document_by_sha(sha)
            reuse = doc and doc["text"] and not self.recompute and (
                doc["ocr_status"] == "done" or (doc["ocr_status"] == "suspect" and not self.force))
            if reuse:
                document_id, text = doc["id"], doc["text"]
                progress.note(f"текст взят из общего документа {document_id} (файл уже читался)")
            else:
                async with progress.step(f"чтение текста ({ext}{', распознавание сканов' if ext == '.pdf' else ''})"):
                    text = await self._read_text(tmp_path, file_name, ext)
                if text and OCR_ERROR_RE.search(text):
                    # Модель OCR была недоступна (перезапуск, 404/5xx) — часть страниц потеряна. Такой текст
                    # не сохраняем и не анализируем: файл получит pipeline.status = failed и будет взят повторно.
                    raise RuntimeError("OCR: часть страниц не распознана — модель распознавания была недоступна")
                if not text or not text.strip() or NO_TEXT_MARK in text:
                    document_id = await self.repo.upsert_document(sha, None, ext, size, None, ocr_status="failed",
                                                                  ocr_error="Не удалось извлечь текст")
                    await self.repo.link_file(self.env, file_id, document_id, card_id=_card(card_id))
                    return document_id, None, sha, size
                text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                # «suspect» — текст с циклами OCR: в режиме отладки (force) файл будет прочитан заново
                status = "done" if text_quality.assess(text)["status"] == "ok" else "suspect"
                # способ чтения — общий документ с экспертизами (knowledge_store.documents.READER_*)
                reader = "rm_pdf_text" if ext == ".pdf" else "file_reader"
                document_id = await self.repo.upsert_document(sha, text_sha, ext, size, text, ocr_status=status,
                                                              reader=reader)
            await self.repo.link_file(self.env, file_id, document_id, card_id=_card(card_id))
            return document_id, text, sha, size
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def _cached(self, document_id: int, analysis_type: str, produce: Callable[[], Awaitable[Any]],
                      compact: Optional[Callable[[Any], Any]] = None,
                      keep: Optional[Callable[[Any], bool]] = None, version_key: Optional[str] = None,
                      version: Optional[str] = None, model: Optional[str] = None,
                      fallback_doc: Optional[int] = None) -> Any:
        """Результат анализа из кэша ``ai_document_analysis`` или новый (с сохранением).

        ``fallback_doc`` — документ прошлой редакции: при анализе изменений его результат (если он есть
        для той же версии промпта и модели) берётся вместо нового запроса к модели и сохраняется для
        текущего документа.

        Args:
            document_id: ``ai_documents.id``.
            analysis_type: Тип анализа (ключ кэша).
            produce: Корутина-фабрика нового результата.
            compact: Функция компактного вида (по умолчанию — сам результат).
            keep: Условие сохранения в кэш (например, не кэшировать неполный риск-анализ).
            version_key: Ключ версии промпта в ``self.versions`` (по умолчанию — ``analysis_type``).
            version: Явная версия (например, версия промпта + набор допустимых кодов).
            model: Модель, выполняющая анализ (ключ кэша; по умолчанию — модель извлечения).
            fallback_doc: ``ai_documents.id`` прошлой редакции (анализ изменений).

        Returns:
            Any: Результат (``None``, если анализ не дал результата).
        """
        version = version or self.versions[version_key or analysis_type]
        model = model or self.extraction_model
        title = progress.analysis_title(analysis_type)
        if not self.recompute:
            cached = await self.repo.get_analysis(document_id, analysis_type, version, model)
            if cached:
                progress.note(f"{title}: из кэша")
                return cached["result"]
        result = None
        stats = getattr(self, "stats", None)
        if fallback_doc and not self.recompute:
            result = await self._get_cached(fallback_doc, analysis_type, version, model)
            if result is not None:
                progress.note(f"{title}: взято из прошлой редакции (текст почти не изменился)")
            if result is not None and stats is not None:
                stats["reused_previous"][analysis_type.split(":")[0]] = stats["reused_previous"].get(analysis_type.split(":")[0], 0) + 1
        if result is None:
            if stats is not None:
                stats["produced"][analysis_type.split(":")[0]] = stats["produced"].get(analysis_type.split(":")[0], 0) + 1
            async with progress.step(f"{title}: запрос к модели {model}"):
                result = await produce()
        if result and (keep is None or keep(result)):
            await self.repo.save_analysis(document_id, analysis_type, version, model, result,
                                          compact(result) if compact else result,
                                          result.get("confidence") if isinstance(result, dict) and isinstance(result.get("confidence"), (int, float)) else None)
        return result

    async def _get_cached(self, document_id: Optional[int], analysis_type: str, version: str, model: str) -> Any:
        """Результат анализа документа из кэша или ``None``."""
        if not document_id:
            return None
        cached = await self.repo.get_analysis(document_id, analysis_type, version, model)
        return cached["result"] if cached else None

    async def _doc_type(self, document_id: int, chunks: List[str], file_name: str,
                        extraction: Optional[Dict[str, Any]], row: Dict[str, Any],
                        print_kind: Optional[Dict[str, Any]] = None, fallback_doc: Optional[int] = None):
        """Тип документа (``doc_kind``).

        * печатная форма ЕИС — по тегу XML-источника, без запроса к модели;
        * иначе ``TypeDetector`` по первому чанку с подсказкой по имени файла: если детектор не уверен
          (< 0,85) и расходится с именем, берётся тип по имени («Обоснование НМЦК.pdf»);
        * проект контракта в составе извещения (владелец не контракт) приводится к ``docProjContractFiles``.

        Returns:
            tuple: ``(type_info, type_decode)``; в ``type_info`` — ``type_source`` и ``print_form``.
        """
        from evaluate_documents.type_data_extractor import ALLOWED_DOC_TYPES, DOCUMENT_TYPE_MAPPING

        if print_kind:
            info = {"detected_type": print_kind["detected_type"], "confidence": 1.0, "issues": [],
                    "key_indicators": [], "type_source": "print_form", "print_form": print_kind}
            return info, print_kind["type_decode"]

        info = await self._cached(document_id, "doc_type",
                                  lambda: self.type_detector.detect_document_type(content=chunks[0], filename=file_name),
                                  model=self.extraction_model, fallback_doc=fallback_doc)
        info = dict(info or {})
        name = ((extraction or {}).get("raw_data") or {}).get("document_name") or ""
        if isinstance(name, dict):
            name = name.get("value") or ""
        fallback = self.type_detector.detect_document_type_by_name(name or file_name) or "docDopMaterialsFiles"
        detected, source = doc_kind.resolve_type(info, doc_kind.type_by_name(file_name), ALLOWED_DOC_TYPES, fallback)
        if (row.get("owner_type") or "") != "contract" and detected in CONTRACT_DOC_TYPES:
            detected = "docProjContractFiles"
        if source == "file_name" and info.get("detected_type") and info.get("detected_type") != detected:
            info.setdefault("issues", []).append(
                f"Модель определила тип «{info.get('detected_type')}» (уверенность {info.get('confidence')}), "
                f"принят тип по имени файла")
        info["detected_type"] = detected
        info["type_source"] = source
        return info, DOCUMENT_TYPE_MAPPING.get(detected)

    async def _load_previous(self, previous: Dict[str, Any], sha: str, file_name: str) -> Dict[str, Any]:
        """Текст прошлой редакции: из ``ai_documents`` (если она анализировалась), иначе файл скачивается и читается.

        Args:
            previous: Строка предыдущей версии файла.
            sha: SHA-256 текущего файла.
            file_name: Имя файла.

        Returns:
            Dict[str, Any]: ``base`` (поля ``version_changes``), ``doc_id``, ``text`` (``None`` — нет текста),
            ``identical`` (тот же файл), ``error``.
        """
        prev_id = int(previous["id"])
        base = {"previous_file_id": prev_id, "previous_eis_version": previous.get("eis_version"),
                "matched_by": previous.get("match"), "previous_xml_id": previous.get("previous_xml_id")}
        out: Dict[str, Any] = {"base": base, "doc_id": None, "text": None, "identical": False, "error": None}
        if previous.get("content_sha256") and previous["content_sha256"] == sha:
            out["identical"] = True
            return out
        link = await self.repo.get_link(self.env, prev_id)
        doc = None
        if link:
            out["doc_id"] = link["document_id"]
            if link["content_sha256"] == sha:
                out["identical"] = True
                return out
            doc = await self.repo.get_document(link["document_id"])
            out["text"] = doc["text"] if doc else None
        purged = bool(link) and out["text"] is None and bool(doc and doc.get("text_purged_at"))
        if (not link or purged) and previous.get("url"):
            # связи нет или текст прошлой редакции очищен по сроку хранения — читаем файл заново
            try:
                doc_id, prev_text, prev_sha, _ = await self._load_document(prev_id, previous["url"],
                                                                           previous.get("file_name") or file_name)
                out.update(doc_id=doc_id, text=prev_text, identical=prev_sha == sha)
            except FileUnavailable as e:
                out["error"] = f"Предыдущая версия недоступна: {e}"
        if out["text"] is None and not out["identical"] and not out["error"]:
            out["error"] = "Нет текста предыдущей версии"
        return out

    async def _version_changes(self, previous: Dict[str, Any], prev_ctx: Dict[str, Any], diff: Optional[Dict[str, Any]],
                               document_id: int, file_name: str, type_decode: Optional[str], use_llm: bool = True):
        """Сравнение с прошлой редакцией: текстовый diff + смысловой diff модели + риски DOC-011 / DOC-012 / AI-004.

        Для печатных форм (``use_llm=False``) — только текстовый diff: изменения разбирает анализ XML-события.

        Returns:
            tuple: ``(version_changes, risks)``.
        """
        base = prev_ctx["base"]
        if prev_ctx.get("identical") or (diff and diff["identical"]):
            return {**base, "identical": True, "similarity": 1.0}, []
        if prev_ctx.get("error") or diff is None:
            return {**base, "error": prev_ctx.get("error") or "Нет текста предыдущей версии"}, []
        result = {**base, "identical": False, "similarity": diff["similarity"], "stats": diff["stats"],
                  "fragments_total": diff.get("fragments_total", 0)}
        llm = None
        if use_llm:
            try:
                llm = await self._cached(document_id, f"version_diff:{prev_ctx.get('doc_id')}",
                                         lambda: self.differ.analyze(diff, file_name, type_decode),
                                         version_key="version_diff", model=self.model)
            except Exception as e:  # noqa: BLE001 — без смыслового diff остаётся текстовый
                logger.error(f"Смысловое сравнение редакций файла не удалось: {e}")
        if llm:
            result.update({"summary": llm.get("summary"), "risk_relevant": bool(llm.get("risk_relevant")),
                           "changes": llm.get("changes") or []})
        else:
            result["fragments"] = diff["fragments"][:10]
        return result, version_diff.version_risks(llm, int(previous["id"]), diff.get("similarity"))

    async def _incremental_risks(self, prev_risk: Dict[str, Any], prev_text: str, text: str, file_name: str,
                                 context: Dict[str, Any], allowed: List[str]) -> Dict[str, Any]:
        """Риски новой редакции по изменениям: перенос рисков с сохранившимися цитатами + поиск в изменённых абзацах.

        Args:
            prev_risk: Полный риск-анализ прошлой редакции.
            prev_text: Текст прошлой редакции.
            text: Текст новой редакции.
            file_name: Имя файла.
            context: Контекст риск-анализа.
            allowed: Допустимые коды.

        Returns:
            Dict[str, Any]: Результат в формате ``DocumentRiskAnalyzer.analyze`` (``incremental = True``).
        """
        kept, dropped = self.risk_analyzer.carry_over(prev_risk, text)
        changed = version_diff.changed_text(prev_text, text)
        fresh = None
        if len(changed["text"]) > version_diff.MAX_CHANGED_CHARS:
            # изменений слишком много — анализируем документ целиком, перенос не нужен
            kept, dropped = [], list((prev_risk or {}).get("risks") or [])
            fresh = await self.risk_analyzer.analyze(text, file_name, context, codes=allowed)
        elif changed["text"].strip() and allowed:
            ctx = {**context, "note": "Передан не весь документ, а только изменённые в новой редакции абзацы "
                                      "(с соседними абзацами для контекста)"}
            fresh = await self.risk_analyzer.analyze(changed["text"], file_name, ctx, codes=allowed)
        result = self.risk_analyzer.merge_results(kept, fresh, allowed)
        result["changed_share"] = changed["share"]
        result["not_found_in_new_version"] = [r.get("code") for r in dropped]
        logger.info(f"{file_name}: анализ изменений — перенесено {len(kept)}, снято {len(dropped)}, "
                    f"найдено в изменениях {result['found_in_changes']} (изменено {changed['share']:.0%})")
        return result

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
                async with progress.step("запись результата во внешнюю БД"):
                    await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, file_id, ai_analysis)
                response["pushed"] = True
            except Exception as e:  # noqa: BLE001 — результат сохранён у нас, следующий запуск дозапишет без LLM
                logger.error(f"Не удалось записать ai_analysis файла {file_id}: {e}")
                response["push_error"] = str(e)
        async with progress.step("сохранение результата во внутренней БД"):
            await self._save_internal(file_id, ai_analysis, response["pushed"], response.get("push_error"))
        return response

    async def _save_internal(self, file_id: int, ai: Dict[str, Any], pushed: bool, error: Optional[str]) -> None:
        """Сохраняет ``ai_analysis`` файла во внутренней БД (``ai_rm_results``); сбой не ломает анализ."""
        if not hasattr(getattr(self, "repo", None), "save_rm_result"):
            return
        try:
            await self.repo.save_rm_result(self.env, "file", file_id, ai, pushed, error)
        except Exception as e:  # noqa: BLE001 — например, до миграции 009
            logger.warning(f"Результат файла {file_id} не сохранён во внутренней БД: {type(e).__name__}: {e}")

    async def _internal(self, ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Сохранённые у нас результаты файлов (``ai_rm_results``); пустой словарь, если таблицы нет."""
        if not ids or not hasattr(getattr(self, "repo", None), "get_rm_results"):
            return {}
        try:
            return await self.repo.get_rm_results(self.env, "file", ids)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Внутренние результаты файлов не получены: {type(e).__name__}: {e}")
            return {}

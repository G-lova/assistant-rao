"""Анализ XML-документов ЕИС (``risk_monitoring_eis_xml_sources``) как событий риск-мониторинга.

Для каждого XML:

1. метаданные, предыдущая версия того же документа и архивы — из внешней БД;
2. XML читается из архива ЕИС (``entry_name``), архив скачивается один раз на запуск;
3. разбор и сравнение с предыдущей версией (``xml_events``): изменения «было → стало» с процентом,
   тип события и подтипы, изменения приложенных файлов, ключевые сведения;
4. для XML без закупки — предложение привязки (номер закупки → номер контракта → ИКЗ → заказчик по ИКУ);
   PHP применяет ``ai_analysis.link``;
5. детерминированные риски (изменение НМЦК/цены, сроков, доп. соглашение, расторжение, уклонение …);
6. ``EventAnalyzer`` (LLM): суть, оценка изменений, значимость, риски по каталогу;
7. запись ``ai_analysis`` в ``risk_monitoring_eis_xml_sources`` через API.

Повторного анализа нет: XML с ``pipeline.status = completed`` пропускается (кроме режима отладки).

Внешняя БД — пакетно, один раз на запуск: метаданные всех XML, их предыдущие версии, файлы всех событий, привязка
всех XML без закупки (номера закупок → номера контрактов → ИКЗ → заказчики) — по запросу на шаг для всех XML сразу
(``RunCache``; раньше — запросы на каждый XML). Общий ``RunCache`` ежедневного прогона делит эти данные и архивы
с анализатором файлов.
"""
import asyncio
import datetime
import os
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from configs import settings
from configs.config import Config
from configs.logger import get_logger
from risk_monitoring import progress
from risk_monitoring import passport, risk_catalog, rm_sql, xml_events
from risk_monitoring.run_cache import RunCache, chunks

logger = get_logger(__name__)

STAGE = "S1a"
EXTERNAL_TABLE = "risk_monitoring_eis_xml_sources"

# Срок хранения текстов РМ (решение 13.10.2026): закрытие закупки/контракта + 12 месяцев
CLOSING_EVENTS = frozenset({"CON-006", "CON-007", "PUR-008", "PUR-016"})   # исполнение, расторжение, отмены
REOPEN_EVENTS = frozenset({"PUR-009"})                                        # закупка возобновлена


def event_date(value: Any) -> datetime.datetime:
    """Дата события для срока хранения: ``exact_date`` выгрузки или текущий момент (UTC).

    Args:
        value: ``date`` / ``datetime`` / строка ``YYYY-MM-DD…`` / ``None``.

    Returns:
        datetime.datetime: Момент с часовым поясом UTC.
    """
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day, tzinfo=datetime.timezone.utc)
    try:
        d = datetime.date.fromisoformat(str(value)[:10])
        return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return datetime.datetime.now(datetime.timezone.utc)
MAX_ATTEMPTS = 3


def _none(v: Any) -> Any:
    """NaN из pandas → ``None``, numpy-числа → Python-числа."""
    try:
        if v != v:  # NaN
            return None
    except Exception:  # noqa: BLE001
        pass
    return v.item() if hasattr(v, "item") else v


def _int(v: Any) -> Optional[int]:
    """Целое или ``None``."""
    v = _none(v)
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class ArchiveCache:
    """Кэш архивов ЕИС на время запуска: каждый архив скачивается один раз.

    Args:
        parser: ``CloudStorageParser`` (метод ``_download_http_file``).
    """

    def __init__(self, parser: Any):
        """Создаёт пустой кэш."""
        self.parser = parser
        self.paths: Dict[str, str] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        # открытый ZIP и указатель «имя → запись» на архив: в выгрузке за день — до десятков тысяч XML в одном
        # архиве, и открывать его заново (с разбором оглавления) на каждый XML — квадратичная работа
        self.zips: Dict[str, Tuple[zipfile.ZipFile, Dict[str, str], Any]] = {}

    async def read(self, url: str, entry_name: str) -> bytes:
        """Байты XML из архива.

        Args:
            url: Ссылка на архив в хранилище внешней системы.
            entry_name: Путь XML внутри ZIP.

        Returns:
            bytes: Содержимое XML.

        Raises:
            FileNotFoundError: Архив не скачался или в нём нет такого XML.
        """
        lock = self.locks.setdefault(url, asyncio.Lock())
        async with lock:
            if url not in self.paths:
                res = await self.parser._download_http_file(url=url, source="inner_archives",
                                                             original_filename=url.rsplit("/", 1)[-1] or "archive.zip")
                if not res or res.get("status") != "success":
                    raise FileNotFoundError(f"архив не скачан: {(res or {}).get('error')}")
                self.paths[url] = res["file_path"]
        path = self.paths[url]
        return await asyncio.to_thread(self._read_cached, path, entry_name)

    def _read_cached(self, path: str, entry_name: str) -> bytes:
        """Читает XML из уже открытого архива (открывается один раз; чтение под блокировкой архива)."""
        import threading

        if path not in self.zips:
            zf = zipfile.ZipFile(path)
            index: Dict[str, str] = {}
            for n in zf.namelist():
                index.setdefault(n, n)
                index.setdefault(n.rsplit("/", 1)[-1], n)
            self.zips[path] = (zf, index, threading.Lock())
        zf, index, lock = self.zips[path]
        name = index.get(entry_name) or index.get(entry_name.rsplit("/", 1)[-1])
        if name is None:
            raise FileNotFoundError(f"в архиве нет {entry_name}")
        with lock:
            info = zf.getinfo(name)
            if info.file_size > xml_events.MAX_XML_BYTES:
                raise ValueError(f"{entry_name}: XML больше {xml_events.MAX_XML_BYTES} байт")
            return zf.read(name)

    @staticmethod
    def _read_entry(path: str, entry_name: str) -> bytes:
        """Читает один XML из ZIP без распаковки остальных (точное имя или совпадение по окончанию пути)."""
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            name = entry_name if entry_name in names else next(
                (n for n in names if n.endswith("/" + entry_name) or n.rsplit("/", 1)[-1] == entry_name.rsplit("/", 1)[-1]), None)
            if name is None:
                raise FileNotFoundError(f"в архиве нет {entry_name}")
            info = zf.getinfo(name)
            if info.file_size > xml_events.MAX_XML_BYTES:
                raise ValueError(f"{entry_name}: XML больше {xml_events.MAX_XML_BYTES} байт")
            return zf.read(name)

    def close(self) -> None:
        """Закрывает открытые архивы и удаляет скачанные."""
        for zf, _, _ in self.zips.values():
            try:
                zf.close()
            except Exception:  # noqa: BLE001
                pass
        self.zips.clear()
        for p in self.paths.values():
            try:
                os.unlink(p)
            except OSError:
                pass
        self.paths.clear()


class XmlEventAnalyzer:
    """Анализ XML ЕИС и запись события во внешнюю БД.

    Args:
        http_manager: Общий ``HTTPClientManager``.
        environment: Окружение внешней системы.
        push_results: Записывать ли результат во внешнюю БД.
        force: Анализировать уже проанализированные XML (только в режиме отладки).
        use_llm: Вызывать ли ``EventAnalyzer``.
        concurrency: Сколько XML обрабатывать одновременно.
    """

    def __init__(self, http_manager: Any, environment: str, push_results: bool = True, force: bool = False,
                 use_llm: bool = True, concurrency: int = 3, repo: Any = None, results_repo: Any = None,
                 run_cache: Optional[RunCache] = None):
        """Создаёт компоненты: доступ к внешней БД, загрузчик архивов, LLM, API записи.

        ``repo`` (``configs.ai_repo.AIRepo``) нужен для срока хранения текстов: закрытие закупки/контракта
        сокращает срок ссылок её файлов. Без него события разбираются как прежде.

        ``results_repo`` (``AIRepo``) — внутреннее хранилище результатов (``ai_rm_results``): ``ai_analysis``
        XML сохраняется у нас; если во внешней БД результата нет, а у нас он есть — дозаписывается без анализа;
        резюме и тип файлов, которых нет во внешней БД, берутся из сохранённых у нас результатов файлов.

        ``run_cache`` — общий кэш прогона (строки внешней БД, архивы); без него — свой на запуск.
        """
        from configs.data_fetcher import DataFetcher
        from configs.llm_client import get_llm
        from configs.parsing import CloudStorageParser
        from risk_monitoring.event_analyzer import EventAnalyzer
        from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

        cfg = Config()
        db = cfg.get_database_config(environment)
        self.env = (environment or cfg.APP_ENV).lower()
        self.repo = repo
        self.results_repo = results_repo if results_repo is not None else repo
        self.internal: Dict[int, Dict[str, Any]] = {}
        self.storage_path = db.storage_path
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)
        self.cache = run_cache or RunCache()
        self.own_archives = self.cache.archives is None
        if self.own_archives:
            self.cache.archives = ArchiveCache(CloudStorageParser(http_manager, None))
        self.archives = self.cache.archives
        from risk_monitoring.llm_json import reasoning_llm

        # события разбирает модель для смысловых задач (RM_REASONING_MODEL), по умолчанию — get_llm()
        client, self.model = reasoning_llm(*get_llm())
        self.event_analyzer = EventAnalyzer(client, self.model) if use_llm else None
        self.risk_api = RiskMonitoringAPI(http_manager, environment)
        self.push_results = push_results
        self.force = force
        self.semaphore = asyncio.Semaphore(concurrency)
        # кому из событий нужна модель (ежедневный прогон: заказчики на мониторинге); None — всем
        self.llm_filter: Optional[Callable[[Dict[str, Any]], bool]] = None
        self.versions = {"event_analyzer": self.event_analyzer.prompt_version if self.event_analyzer else None,
                         "catalog": risk_catalog.CATALOG_VERSION}

    async def _query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Выполняет запрос ``queries/rm/<name>`` и возвращает строки как словари."""
        sql, bindings = rm_sql.render(name, head=head, **lists)
        df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [{k: _none(v) for k, v in r.items()} for r in df.to_dict("records")] if not df.empty else []

    # ------------------------------------------------------------------ вход

    async def analyze_ids(self, xml_ids: List[int]) -> List[Dict[str, Any]]:
        """Анализирует XML по id.

        Args:
            xml_ids: id ``risk_monitoring_eis_xml_sources``.

        Returns:
            List[Dict[str, Any]]: По одному результату на id: ``status`` (``done`` / ``skipped`` /
            ``not_found`` / ``error``), ``pushed``, ``ai_analysis``.
        """
        ids = list(dict.fromkeys(int(i) for i in xml_ids))
        try:
            progress.subject(f"XML ({len(ids)})")
            async with progress.step("чтение сведений о XML из внешней БД") as st:
                rows = await self._meta(ids)
                st["detail"] = f"найдено {len(rows)} из {len(ids)}"
            found = {int(r["id"]): r for r in rows}
            async with progress.step("поиск предыдущих версий XML"):
                previous = await self._previous(list(found)) if found else {}
            self.internal = await self._internal("xml", list(found))
            todo = [found[i] for i in ids if i in found and self._will_prepare(found[i])]
            async with progress.step("чтение XML, файлы событий и привязка — пакетно") as st:
                await self.prefetch(todo, previous)
                st["detail"] = f"XML к разбору {len(todo)}; запросов к внешней БД: {self._cache().queries}"
            # 1) разбор XML, diff, правила — без модели
            prepared = await asyncio.gather(*(self._guarded(found[i], previous.get(i)) for i in ids if i in found))
            ctxs = [p for p in prepared if p.get("status") == "prepared"]
            self._defer_llm(ctxs)
            # 2) модель — одним запросом на закупку (контракт) для событий, где она нужна
            llm = await self._llm_by_purchase([c for c in ctxs if c["needs_llm"]])
            # 3) сборка ai_analysis и запись
            finished = {c["xml_id"]: await self._finish_guarded(c, llm.get(c["xml_id"])) for c in ctxs}
            reasons: Dict[str, int] = {}
            for c in ctxs:
                reasons[c["llm_reason"]] = reasons.get(c["llm_reason"], 0) + 1
            self.stats = {"events": len(ctxs), "with_llm": sum(1 for c in ctxs if c["needs_llm"]), "reasons": reasons}
            results = [finished.get(p["xml_id"], p) for p in prepared]
            return list(results) + [{"xml_id": i, "status": "not_found"} for i in ids if i not in found]
        finally:
            if getattr(self, "own_archives", True):
                self.archives.close()

    async def pending_files(self, xml_ids: List[int], include_done: bool = False) -> List[int]:
        """Файлы, указанные в XML (вложения и печатная форма) или привязанные к нему, ещё не проанализированные.

        Используется опцией ``analyze_files``: файлы события анализируются до самого события, чтобы его анализ
        получил резюме и риски файлов.

        Args:
            xml_ids: id XML.
            include_done: Вернуть и уже проанализированные (режим отладки, ``force``).

        Returns:
            List[int]: id файлов ``risk_monitoring_files`` без повторов.
        """
        rows = await self._meta(list(xml_ids))
        previous = await self._previous([int(r["id"]) for r in rows]) if rows else {}
        await self.prefetch(rows, previous)
        ids = {int(r["id"]) for r in rows}
        urls = set()
        for r in rows:
            urls |= self._urls(int(r["id"]), print_forms=True)
        files = [f for f in self._cache().file_rows.values()
                 if _int(f.get("xml_source_id")) in ids or f.get("source_url") in urls]
        out = [int(f["id"]) for f in sorted(files, key=lambda f: int(f["id"]))
               if include_done or f.get("rm_status") != "completed"]
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------ пакетная загрузка (RunCache)
    def _cache(self) -> RunCache:
        """Кэш прогона (создаётся при первом обращении — и у объектов, собранных без ``__init__``)."""
        c = self.__dict__.get("cache")
        if c is None:
            c = self.cache = RunCache(self.__dict__.get("archives"))
        return c

    def _will_prepare(self, row: Dict[str, Any]) -> bool:
        """XML будет разбираться в этом запуске (не пропущен как проанализированный или исчерпавший попытки)."""
        if self.force:
            return True
        if row.get("rm_status") == "completed":
            return False
        stored = self.internal.get(int(row["id"]))
        if stored and stored.get("status") == "completed" and not stored.get("debug"):
            return False
        return not (row.get("rm_status") == "failed" and (_int(row.get("rm_attempt")) or 0) + 1 > MAX_ATTEMPTS)

    async def _meta(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Строки ``xml_meta.sql`` — каждый XML запрашивается один раз за прогон."""
        got = await self._cache().one_each(self._query, "xml_meta.sql", ids, "id", head=[self.storage_path])
        return [got[int(i)] for i in ids if int(i) in got]

    async def _parse(self, x: Dict[str, Any]) -> Tuple[str, Any, Any, int]:
        """Разобранный XML (тег, версия, тело, размер) — из архива один раз за прогон.

        Raises:
            Exception: XML не прочитан или не разобран (ошибка сохраняется и повторяется без нового чтения).
        """
        c = self._cache()
        key = int(x["id"])
        if key not in c.parsed:
            try:
                raw = await self.archives.read(x["archive_url"], x["entry_name"])
                tag, version, body = xml_events.parse_xml(raw)
                c.parsed[key] = (tag, version, body, len(raw))
            except Exception as e:  # noqa: BLE001 — запоминаем: повторное чтение даст то же
                c.parsed[key] = e
        got = c.parsed[key]
        if isinstance(got, Exception):
            raise got
        return got

    def _urls(self, xml_id: int, print_forms: bool = False) -> set:
        """Ссылки вложений (и печатных форм) разобранного XML."""
        from risk_monitoring import file_versions

        got = self._cache().parsed.get(int(xml_id))
        if not got or isinstance(got, Exception):
            return set()
        body = got[2]
        urls = {a["url"] for a in xml_events.attachments(body)}
        return urls | set(file_versions.print_form_urls(body)) if print_forms else urls

    async def prefetch(self, rows: List[Dict[str, Any]], previous: Dict[int, Dict[str, Any]]) -> None:
        """Пакетно готовит всё, что нужно для разбора XML: читает XML и предыдущие версии, одним запросом — файлы
        всех событий, одним обращением — их результаты у нас, по запросу на шаг — привязку XML без закупки.

        Args:
            rows: Строки ``xml_meta.sql``, которые будут разбираться.
            previous: ``id XML → предыдущая версия``.
        """
        if not rows:
            return
        sem = asyncio.Semaphore(8)

        async def parse(x: Dict[str, Any]) -> None:
            """Чтение одного XML (ошибка остаётся в кэше и всплывёт при разборе)."""
            async with sem:
                try:
                    await self._parse(x)
                except Exception:  # noqa: BLE001
                    pass

        prevs = [previous[int(r["id"])] for r in rows if previous.get(int(r["id"]))]
        await asyncio.gather(*(parse(x) for x in list(rows) + prevs))
        covered = self._cache().covered_cards
        ids = [int(r["id"]) for r in rows] + [int(p["id"]) for p in prevs]
        urls = set()
        for r in rows:
            # файлы закупки уже загружены целиком (ежедневный прогон: daily_files.sql) — ссылки не запрашиваются
            if _int(r.get("risk_monitoring_contract_id")) not in covered:
                urls |= self._urls(int(r["id"]), print_forms=True)
        try:
            await self._load_files(ids, sorted(urls))
            await self._load_file_internal()
        except Exception as e:  # noqa: BLE001 — файлы событий не обязательны
            logger.warning(f"Файлы XML не получены: {type(e).__name__}: {e}")
        orphans = []
        for r in rows:
            if r.get("risk_monitoring_contract_id") is not None or int(r["id"]) in self._cache().links:
                continue
            try:
                tag, _, body, _ = await self._parse(r)
            except Exception:  # noqa: BLE001
                continue
            orphans.append((int(r["id"]), xml_events.extract_facts(tag, body), r))
        if orphans:
            await self._link_batch(orphans)

    async def _load_files(self, xml_ids: List[int], urls: List[str]) -> None:
        """Файлы по ``xml_source_id`` и по ссылкам — только ещё не запрошенные, пачками (``xml_files.sql``)."""
        c = self._cache()
        ids = [i for i in dict.fromkeys(int(i) for i in xml_ids) if i not in c.file_ids]
        new_urls = [u for u in dict.fromkeys(urls) if u and u not in c.file_urls]
        id_parts, url_parts = chunks(ids), chunks(new_urls)
        for n in range(max(len(id_parts), len(url_parts))):
            part_ids = id_parts[n] if n < len(id_parts) else []
            part_urls = url_parts[n] if n < len(url_parts) else []
            c.count("xml_files.sql")
            for r in await self._query("xml_files.sql", ids=part_ids, urls=part_urls):
                if r.get("id") is not None:
                    c.file_rows[int(r["id"])] = r
            c.file_ids.update(part_ids)
            c.file_urls.update(part_urls)

    async def _load_file_internal(self) -> None:
        """Наши результаты файлов, у которых во внешней БД нет анализа, — одним обращением за все файлы."""
        c = self._cache()
        checked = c.__dict__.setdefault("file_internal_checked", set())
        found = c.__dict__.setdefault("file_internal", {})
        fresh = getattr(self, "file_results", None) or {}
        missing = [i for i, r in c.file_rows.items() if i not in checked and i not in fresh
                   and not (r.get("detected_type") or r.get("resume"))]
        if not missing:
            return
        for fid, stored in (await self._internal("file", missing)).items():
            if stored.get("status") == "completed":
                found[fid] = stored["ai_analysis"]
        checked.update(missing)

    async def _previous(self, ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Предыдущая версия каждого XML (максимальная версия меньше текущей) — один запрос на все XML прогона."""
        try:
            grouped = await self._cache().keyed(self._query, "xml_previous.sql", ids, "current_id",
                                                head=[self.storage_path], size=settings.RM_PREVIOUS_CHUNK)
        except Exception as e:  # noqa: BLE001 — без предыдущей версии событие всё равно анализируется
            logger.warning(f"Предыдущие версии XML не получены: {e}")
            return {}
        best: Dict[int, Dict[str, Any]] = {}
        for cur, rows in grouped.items():
            for r in rows:
                if cur not in best or (_int(r.get("eis_version")) or -1) > (_int(best[cur].get("eis_version")) or -1):
                    best[int(cur)] = r
        return best

    async def _guarded(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Один XML: правило «без повторного анализа», семафор, обработка ошибок."""
        xml_id = int(row["id"])
        attempt = (_int(row.get("rm_attempt")) or 0) + 1
        progress.subject(f"XML {xml_id} «{row.get('entry_name') or ''}»")
        if not self.force and not self._deferred_now_needed(row):
            if row.get("rm_status") == "completed":
                progress.note("уже проанализирован во внешней БД — пропуск")
                return {"xml_id": xml_id, "status": "skipped", "reason": "already_analyzed"}
            stored = self.internal.get(xml_id)
            if stored and stored.get("status") == "completed" and not stored.get("debug"):
                return await self._reuse_internal(xml_id, stored)
            if row.get("rm_status") == "failed" and attempt > MAX_ATTEMPTS:
                return {"xml_id": xml_id, "status": "skipped", "reason": "max_attempts"}
        async with self.semaphore:
            try:
                return await self._prepare(row, previous, attempt)
            except Exception as e:  # noqa: BLE001 — сбой одного XML не останавливает остальные
                return await self._failure(xml_id, attempt, e)

    async def _internal(self, kind: str, ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Сохранённые у нас результаты (``ai_rm_results``); пустой словарь без хранилища или таблицы."""
        if not ids or self.results_repo is None or not hasattr(self.results_repo, "get_rm_results"):
            return {}
        try:
            return await self.results_repo.get_rm_results(self.env, kind, ids)
        except Exception as e:  # noqa: BLE001 — например, до миграции 009
            logger.warning(f"Внутренние результаты ({kind}) не получены: {type(e).__name__}: {e}")
            return {}

    async def _save_internal(self, xml_id: int, ai: Dict[str, Any], pushed: bool, error: Optional[str]) -> None:
        """Сохраняет ``ai_analysis`` XML во внутренней БД; сбой не ломает анализ."""
        if self.results_repo is None or not hasattr(self.results_repo, "save_rm_result"):
            return
        try:
            await self.results_repo.save_rm_result(self.env, "xml", xml_id, ai, pushed, error)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Результат XML {xml_id} не сохранён во внутренней БД: {type(e).__name__}: {e}")

    async def _reuse_internal(self, xml_id: int, stored: Dict[str, Any]) -> Dict[str, Any]:
        """XML уже разобран, но во внешней БД результата нет: дозаписывает сохранённый, без повторного разбора.

        Returns:
            Dict[str, Any]: ``status = done``, ``source = internal``.
        """
        ai = stored["ai_analysis"]
        result = {"xml_id": xml_id, "status": "done", "source": "internal", "pushed": False, "ai_analysis": ai}
        progress.note(f"результат есть во внутренней БД (разбор {stored.get('analyzed_at')}) — без повторного разбора")
        if self.push_results:
            try:
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, xml_id, ai)
                result["pushed"] = True
                await self.results_repo.mark_rm_pushed(self.env, "xml", xml_id)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось дозаписать ai_analysis XML {xml_id}: {e}")
                result["push_error"] = str(e)
        logger.info(f"XML {xml_id}: результат из внутренней БД (разбор {stored.get('analyzed_at')}), "
                    f"записан во внешнюю БД: {result['pushed']}")
        return result

    async def _failure(self, xml_id: int, attempt: int, e: BaseException) -> Dict[str, Any]:
        """Результат с ошибкой и запись ``pipeline.status = failed`` (кроме режима отладки)."""
        logger.error(f"Ошибка анализа XML {xml_id}: {e}", exc_info=True)
        result = {"xml_id": xml_id, "status": "error", "error": f"{type(e).__name__}: {e}", "pushed": False}
        if self.push_results and not self.force:
            block = {"pipeline": passport.pipeline_block(STAGE, "failed", self.model, self.versions,
                                                         attempt=attempt, error=result["error"])}
            try:
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, xml_id, block)
                result["pushed"] = True
            except Exception as pe:  # noqa: BLE001
                logger.error(f"Не удалось записать статус ошибки XML {xml_id}: {pe}")
        return result

    def _defer_llm(self, ctxs: List[Dict[str, Any]]) -> None:
        """Откладывает модель для событий, не прошедших ``llm_filter``: событие описывается правилами и шаблоном,
        ``pipeline.llm.deferred`` — причина, по которой модель была бы нужна (для повторного разбора, когда
        заказчик встанет на мониторинг)."""
        if getattr(self, "llm_filter", None) is None:
            return
        n = 0
        for c in ctxs:
            if c.get("needs_llm") and not self.llm_filter(c):
                c.update(needs_llm=False, llm_deferred=c["llm_reason"], llm_reason="not_monitored")
                n += 1
        if n:
            logger.info(f"События XML: модель отложена для {n} (заказчик не на мониторинге)")

    def _deferred_now_needed(self, row: Dict[str, Any]) -> bool:
        """Разбор XML без модели был отложен, а теперь его закупка у заказчика на мониторинге."""
        deferred = row.get("rm_llm_deferred")
        if getattr(self, "llm_filter", None) is None or deferred in (None, "", "null", "None") or deferred != deferred:
            return False
        return bool(self.llm_filter({"row": row, "link": None}))

    async def _finish_guarded(self, ctx: Dict[str, Any], llm: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Сборка и запись события с обработкой ошибок."""
        try:
            return await self._finish(ctx, llm)
        except Exception as e:  # noqa: BLE001
            return await self._failure(ctx["xml_id"], ctx["attempt"], e)

    async def _llm_by_purchase(self, ctxs: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Анализ моделью событий, сгруппированных по закупке (контракту): один запрос на пакет.

        Args:
            ctxs: Подготовленные события, которым нужна модель.

        Returns:
            Dict[int, Dict[str, Any]]: ``xml_id → ответ модели``.
        """
        if not self.event_analyzer or not ctxs:
            return {}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for c in ctxs:
            row = c["row"]
            key = (f"c{row['risk_monitoring_contract_id']}" if row.get("risk_monitoring_contract_id") is not None
                   else f"n{row.get('purchase_number') or row.get('contract_number') or c['xml_id']}")
            groups.setdefault(key, []).append(c)
        out: Dict[int, Dict[str, Any]] = {}

        async def run(key: str, group: List[Dict[str, Any]]) -> None:
            """Пакеты событий одной закупки по порядку версий."""
            group.sort(key=lambda c: (_int(c["row"].get("eis_version")) or 0, c["xml_id"]))
            items = [{"event_id": c["xml_id"], "payload": c["payload"]} for c in group]
            name = group[0]["row"].get("purchase_number") or group[0]["row"].get("contract_number") or key
            progress.subject(f"Закупка {name}")
            for batch in self.event_analyzer.batches(items):
                async with progress.step(f"анализ событий моделью ({len(batch)} XML)"):
                    out.update(await self.event_analyzer.analyze_batch(batch, str(name)))

        await asyncio.gather(*(run(k, g) for k, g in groups.items()))
        calls = sum(len(self.event_analyzer.batches([{"event_id": 0, "payload": c["payload"]} for c in g])) for g in groups.values())
        logger.info(f"События XML: модель нужна {len(ctxs)} событиям, закупок {len(groups)}, пакетов ≈{calls}")
        return out

    # ------------------------------------------------------------- один XML

    async def analyze_one(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]], attempt: int = 1) -> Dict[str, Any]:
        """Полный анализ одного XML и запись события (без объединения с другими событиями закупки).

        Args:
            row: Строка ``xml_meta.sql``.
            previous: Предыдущая версия (``xml_previous.sql``) или ``None``.
            attempt: Номер попытки.

        Returns:
            Dict[str, Any]: ``xml_id``, ``status``, ``pushed``, ``ai_analysis``.
        """
        ctx = await self._prepare(row, previous, attempt)
        llm = None
        if ctx["needs_llm"] and self.event_analyzer:
            llm = await self.event_analyzer.analyze(ctx["payload"], row.get("entry_name") or str(ctx["xml_id"]))
        return await self._finish(ctx, llm)

    async def _prepare(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]], attempt: int = 1) -> Dict[str, Any]:
        """Разбор XML без модели: diff с предыдущей версией, вложения, файлы, тип события, правила,
        привязка «сироты», решение о вызове модели и пакет для неё.

        Args:
            row: Строка ``xml_meta.sql``.
            previous: Предыдущая версия или ``None``.
            attempt: Номер попытки.

        Returns:
            Dict[str, Any]: Контекст события (``status = prepared``, ``needs_llm``, ``llm_reason``, ``payload``).
        """
        xml_id = int(row["id"])
        progress.subject(f"XML {xml_id} «{row.get('entry_name') or ''}»")
        async with progress.step("чтение XML из архива ЕИС") as st:
            tag, xml_version, body, size = await self._parse(row)
            st["detail"] = f"{tag}, {size // 1024} КБ"
        version = _int(row.get("eis_version"))
        version = xml_version if version is None else version

        prev_body = None
        if previous:
            try:
                progress.note(f"предыдущая версия: XML {previous.get('id')} (версия {previous.get('eis_version')})")
                _, _, prev_body, _ = await self._parse(previous)
            except Exception as e:  # noqa: BLE001 — нет предыдущей версии: событие без изменений
                logger.warning(f"XML {xml_id}: предыдущая версия {previous.get('id')} не прочитана: {e}")
                previous = None

        facts = xml_events.extract_facts(tag, body)
        fz = risk_catalog.normalize_fz(row.get("fz")) or risk_catalog.normalize_fz(row.get("card_fz"))
        changes = xml_events.diff(xml_events.strip_ignored(prev_body), xml_events.strip_ignored(body)) if prev_body else []
        atts = xml_events.attachments(body)
        doc_changes = xml_events.document_changes(xml_events.attachments(prev_body) if prev_body else None, atts)
        async with progress.step("файлы события и их результаты анализа") as st:
            all_files = await self._files(xml_id, previous, atts)
            st["detail"] = f"изменений {len(changes)}, файлов {len(all_files)}"
        urls = {a["url"] for a in atts}
        files = [f for f in all_files if f.get("xml_source_id") == xml_id or f.get("source_url") in urls]
        event = xml_events.classify(tag, version, previous is not None, changes, facts, doc_changes)

        link = None
        if row.get("risk_monitoring_contract_id") is None:
            async with progress.step("поиск закупки для XML без привязки") as st:
                link = await self._link_orphan(facts, row)
                st["detail"] = (link or {}).get("method")

        rules = xml_events.rule_risks(event, changes, facts)
        needs_llm, reason = xml_events.llm_decision(event, changes, rules)
        progress.note(f"событие {event.get('code')} «{event.get('title')}», правил сработало {len(rules)}, "
                      f"модель {'нужна' if needs_llm else 'не нужна'} ({reason})")
        payload = None
        if needs_llm:
            payload = xml_events.build_llm_payload(event, facts, changes, doc_changes, files)
            payload["law"] = f"{fz}-ФЗ" if fz else "закон не определён"
        return {"status": "prepared", "xml_id": xml_id, "row": row, "attempt": attempt, "previous": previous,
                "facts": facts, "fz": fz, "changes": changes, "doc_changes": doc_changes, "files": files,
                "all_files": all_files, "event": event, "link": link, "rules": rules,
                "needs_llm": bool(needs_llm and self.event_analyzer), "llm_reason": reason, "payload": payload}

    async def _finish(self, ctx: Dict[str, Any], llm: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Собирает ``ai_analysis`` события и записывает его во внешнюю БД.

        Args:
            ctx: Контекст из :meth:`_prepare`.
            llm: Ответ модели или ``None`` (событие описано шаблоном).

        Returns:
            Dict[str, Any]: ``xml_id``, ``status``, ``pushed``, ``ai_analysis``.
        """
        row, previous, xml_id = ctx["row"], ctx["previous"], ctx["xml_id"]
        progress.subject(f"XML {xml_id} «{row.get('entry_name') or ''}»")
        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions,
                                           _int(previous["id"]) if previous else None, self.force, ctx["attempt"],
                                           export_date=str(row.get("exact_date"))[:10] if row.get("exact_date") else None)
        pipeline["llm"] = {"used": llm is not None, "reason": ctx["llm_reason"]}
        if ctx.get("llm_deferred"):
            pipeline["llm"]["deferred"] = ctx["llm_deferred"]
        ai = xml_events.build_xml_analysis(event=ctx["event"], facts=ctx["facts"], changes=ctx["changes"],
                                           doc_changes=ctx["doc_changes"], files=ctx["files"], link=ctx["link"],
                                           rule_risk_list=ctx["rules"], llm=llm,
                                           previous={"id": previous["id"], "eis_version": _int(previous.get("eis_version"))} if previous else None,
                                           pipeline=pipeline, model=self.model if llm else None,
                                           all_files=ctx["all_files"], fz=ctx["fz"])
        ai["significant_changes_total"] = len(xml_events.significant_changes(ctx["changes"]))
        response = {"xml_id": xml_id, "status": "done", "pushed": False, "ai_analysis": ai}
        retention = await self._update_retention(ctx)
        if retention:
            response["retention"] = retention
        if self.push_results:
            try:
                async with progress.step("запись результата во внешнюю БД"):
                    await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, xml_id, ai)
                response["pushed"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось записать ai_analysis XML {xml_id}: {e}")
                response["push_error"] = str(e)
        async with progress.step("сохранение результата во внутренней БД"):
            await self._save_internal(xml_id, ai, response["pushed"], response.get("push_error"))
        return response

    async def _update_retention(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Срок хранения текстов файлов закупки по событию: закрытие сокращает, возобновление восстанавливает.

        Ошибка не мешает записи события (только предупреждение в журнале).

        Args:
            ctx: Контекст события из :meth:`_prepare`.

        Returns:
            Optional[Dict[str, Any]]: ``{"action", "card_id", "links"}`` или ``None``, если событие не меняет срок.
        """
        code = (ctx.get("event") or {}).get("code")
        if self.repo is None or code not in CLOSING_EVENTS | REOPEN_EVENTS:
            return None
        row = ctx.get("row") or {}
        card = _int(row.get("risk_monitoring_contract_id")) or _int((ctx.get("link") or {}).get("risk_monitoring_contract_id"))
        if not card:
            return None
        try:
            if code in CLOSING_EVENTS:
                n = await self.repo.close_card(self.env, card, event_date(row.get("exact_date")))
                return {"action": "closed", "card_id": card, "links": n}
            n = await self.repo.reopen_card(self.env, card)
            return {"action": "reopened", "card_id": card, "links": n}
        except Exception as e:  # noqa: BLE001 — срок хранения не должен ломать разбор события
            logger.warning(f"Срок хранения файлов закупки {card} не обновлён ({code}): {type(e).__name__}: {e}")
            return None

    async def _files(self, xml_id: int, previous: Optional[Dict[str, Any]], atts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Файлы текущей и предыдущей версии XML (по ``xml_source_id`` и по ссылкам), с кратким результатом анализа.

        Строки берутся из кэша прогона (``prefetch`` запросил их сразу для всех XML); недостающие — дозапрашиваются.
        """
        ids = [xml_id] + ([int(previous["id"])] if previous else [])
        urls = [a["url"] for a in atts][:200]
        c = self._cache()
        try:
            # XML привязанной закупки, файлы которой загружены целиком запросом прогона, — без запроса по ссылкам
            # (прогон 21.10.2026: 81 запрос xml_files.sql на 111 XML)
            covered = xml_id in c.covered_xml and (not previous or int(previous["id"]) in c.covered_xml)
            await self._load_files([] if covered else ids, [] if covered else urls)
            await self._load_file_internal()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Файлы XML {xml_id} не получены: {e}")
            return []
        id_set, url_set = set(ids), set(urls)
        rows = [r for _, r in sorted(c.file_rows.items())
                if _int(r.get("xml_source_id")) in id_set or r.get("source_url") in url_set]
        fresh = {**c.__dict__.get("file_internal", {}), **dict(getattr(self, "file_results", None) or {})}
        out = []
        for r in rows:
            item = {"file_id": _int(r.get("id")), "xml_source_id": _int(r.get("xml_source_id")),
                    "file_name": r.get("file_name"), "source_url": r.get("source_url"),
                    "detected_type": r.get("detected_type"), "resume": r.get("resume"),
                    "total_doc_risk": r.get("total_doc_risk")}
            ai = fresh.get(item["file_id"])
            if ai:
                # файл проанализирован в этой же задаче (analyze_files) или результат есть только у нас:
                # во внешней БД его может не быть (push=false, сбой записи)
                item.update(detected_type=(ai.get("doc_type") or {}).get("detected_type"), resume=ai.get("resume"),
                            total_doc_risk=ai.get("total_doc_risk"))
            out.append(item)
        return out

    async def _link_orphan(self, facts: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
        """Предложение привязки XML без закупки (лестница: номер закупки → номер контракта → ИКЗ → заказчик).

        Обычно уже готово: ``prefetch`` привязывает все XML запуска пакетно (:meth:`_link_batch`).

        Args:
            facts: Сведения XML.
            row: Строка ``xml_meta.sql`` (номера из колонок таблицы — дополнительный источник).

        Returns:
            Dict[str, Any]: ``method``, ``risk_monitoring_contract_id``, ``organisation_id``, ``ambiguous``,
            ``candidates``, ``customer_inn``, ``customer_kpp``. ``method = none`` — привязать не удалось.
        """
        xml_id = _int(row.get("id"))
        links = self._cache().links
        if xml_id is None or xml_id not in links:
            await self._link_batch([(xml_id, facts, row)])
        return links.get(xml_id) or {"method": "none", "risk_monitoring_contract_id": None, "organisation_id": None,
                                     "ambiguous": False, "candidates": []}

    async def _link_batch(self, items: List[Tuple[Optional[int], Dict[str, Any], Dict[str, Any]]]) -> None:
        """Привязка многих XML без закупки: по одному запросу к внешней БД на шаг лестницы для всех XML сразу.

        Шаги: карточки по номерам закупок (``link_by_number``) → карточки по номерам контрактов через другие XML
        (``link_by_contract_number``) → карточки по ИКЗ (``link_by_ikz``, полный просмотр JSON — один раз на запуск)
        → заказчик по ИНН/КПП из ИКЗ (``org_group``). Результат — в ``RunCache.links``.

        Args:
            items: ``(xml_id, сведения XML, строка xml_meta.sql)``.
        """
        links = self._cache().links

        def blank() -> Dict[str, Any]:
            """Пустая привязка."""
            return {"method": "none", "risk_monitoring_contract_id": None, "organisation_id": None,
                    "ambiguous": False, "candidates": []}

        todo: Dict[Any, Dict[str, Any]] = {}
        for xml_id, facts, row in items:
            todo[xml_id] = {"facts": facts, "link": blank(),
                            "purchase": sorted({str(n).strip() for n in (facts.get("purchase_number"), row.get("purchase_number")) if n}),
                            "contract": sorted({str(n).strip() for n in (facts.get("contract_reg_number"), row.get("contract_number")) if n})}
        try:
            nums = sorted({n for t in todo.values() for n in t["purchase"]})
            if nums:
                by_num: Dict[str, List[Dict[str, Any]]] = {}
                for part in chunks(nums):
                    self._cache().count("link_by_number.sql")
                    for c in await self._query("link_by_number.sql", nums=part):
                        by_num.setdefault(str(c.get("number")).strip(), []).append(c)
                for t in todo.values():
                    cards = [c for n in t["purchase"] for c in by_num.get(n, [])]
                    if cards:
                        t["link"].update(method="purchase_number", risk_monitoring_contract_id=_int(cards[0]["id"]),
                                         organisation_id=_int(cards[0].get("risk_monitoring_organisation_id")),
                                         ambiguous=len(cards) > 1, candidates=[_int(c["id"]) for c in cards])
            rest = [t for t in todo.values() if t["link"]["method"] == "none"]
            nums = sorted({n for t in rest for n in t["contract"]})
            if nums:
                by_num = {}
                for part in chunks(nums):
                    self._cache().count("link_by_contract_number.sql")
                    for r in await self._query("link_by_contract_number.sql", nums=part):
                        by_num.setdefault(str(r.get("contract_number")).strip(), []).append(r)
                for t in rest:
                    rows = sorted((r for n in t["contract"] for r in by_num.get(n, [])), key=lambda r: -(_int(r.get("n")) or 0))
                    if rows:
                        t["link"].update(method="contract_number",
                                         risk_monitoring_contract_id=_int(rows[0]["risk_monitoring_contract_id"]),
                                         ambiguous=len({r["risk_monitoring_contract_id"] for r in rows}) > 1,
                                         candidates=[_int(r["risk_monitoring_contract_id"]) for r in rows])
            rest = [t for t in todo.values() if t["link"]["method"] == "none" and t["facts"].get("ikz_parts")]
            ikzs = sorted({t["facts"]["ikz_parts"]["ikz"] for t in rest})
            if ikzs:
                by_ikz: Dict[str, List[Dict[str, Any]]] = {}
                for part in chunks(ikzs):
                    self._cache().count("link_by_ikz.sql")
                    for c in await self._query("link_by_ikz.sql", nums=part):
                        by_ikz.setdefault(str(c.get("ikz")).strip(), []).append(c)
                for t in rest:
                    f = t["facts"]
                    cards = by_ikz.get(f["ikz_parts"]["ikz"], [])
                    card, ambiguous = xml_events.pick_by_ikz(cards, f.get("published_at") or f.get("sign_date"))
                    if card:
                        t["link"].update(method="ikz", risk_monitoring_contract_id=_int(card["id"]),
                                         organisation_id=_int(card.get("risk_monitoring_organisation_id")),
                                         ambiguous=ambiguous, candidates=[_int(c["id"]) for c in cards])
            rest = [t for t in rest if t["link"]["method"] == "none"]
            inns = sorted({t["facts"]["ikz_parts"]["customer_inn"] for t in rest if t["facts"]["ikz_parts"].get("customer_inn")})
            if inns:
                orgs: List[Dict[str, Any]] = []
                for part in chunks(inns):
                    self._cache().count("org_group.sql")
                    orgs += await self._query("org_group.sql", inns=part)
                for t in rest:
                    parts = t["facts"]["ikz_parts"]
                    t["link"].update(customer_inn=parts["customer_inn"], customer_kpp=parts["customer_kpp"])
                    mine = [o for o in orgs if str(o.get("inn") or "").strip() == str(parts["customer_inn"]).strip()
                            and str(o.get("kpp") or "").strip() == str(parts["customer_kpp"] or "").strip()]
                    org = xml_events.canonical_org(mine, datetime.date.today().year)
                    if org:
                        t["link"].update(method="ikz_customer", organisation_id=_int(org["id"]))
        except Exception as e:  # noqa: BLE001 — привязка не обязательна для события
            logger.warning(f"Привязка XML не выполнена: {e}")
            for t in todo.values():
                if t["link"]["method"] == "none":
                    t["link"]["error"] = str(e)
        for xml_id, t in todo.items():
            links[xml_id] = t["link"]


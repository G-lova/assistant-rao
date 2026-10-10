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
"""
import asyncio
import datetime
import os
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from configs.config import Config
from configs.logger import get_logger
from risk_monitoring import passport, risk_catalog, rm_sql, xml_events

logger = get_logger(__name__)

STAGE = "S1a"
EXTERNAL_TABLE = "risk_monitoring_eis_xml_sources"
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
        return await asyncio.to_thread(self._read_entry, path, entry_name)

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
        """Удаляет скачанные архивы."""
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
                 use_llm: bool = True, concurrency: int = 3):
        """Создаёт компоненты: доступ к внешней БД, загрузчик архивов, LLM, API записи."""
        from configs.data_fetcher import DataFetcher
        from configs.llm_client import get_llm
        from configs.parsing import CloudStorageParser
        from risk_monitoring.event_analyzer import EventAnalyzer
        from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

        cfg = Config()
        db = cfg.get_database_config(environment)
        self.storage_path = db.storage_path
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)
        self.archives = ArchiveCache(CloudStorageParser(http_manager, None))
        client, self.model = get_llm()
        self.event_analyzer = EventAnalyzer(client, self.model) if use_llm else None
        self.risk_api = RiskMonitoringAPI(http_manager, environment)
        self.push_results = push_results
        self.force = force
        self.semaphore = asyncio.Semaphore(concurrency)
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
            rows = await self._query("xml_meta.sql", head=[self.storage_path], ids=ids)
            found = {int(r["id"]): r for r in rows}
            previous = await self._previous(list(found)) if found else {}
            results = await asyncio.gather(*(self._guarded(found[i], previous.get(i)) for i in ids if i in found))
            return list(results) + [{"xml_id": i, "status": "not_found"} for i in ids if i not in found]
        finally:
            self.archives.close()

    async def _previous(self, ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Предыдущая версия каждого XML (максимальная версия меньше текущей)."""
        try:
            rows = await self._query("xml_previous.sql", head=[self.storage_path], ids=ids)
        except Exception as e:  # noqa: BLE001 — без предыдущей версии событие всё равно анализируется
            logger.warning(f"Предыдущие версии XML не получены: {e}")
            return {}
        best: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            cur = int(r["current_id"])
            if cur not in best or (_int(r.get("eis_version")) or -1) > (_int(best[cur].get("eis_version")) or -1):
                best[cur] = r
        return best

    async def _guarded(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Один XML: правило «без повторного анализа», семафор, обработка ошибок."""
        xml_id = int(row["id"])
        attempt = (_int(row.get("rm_attempt")) or 0) + 1
        if not self.force:
            if row.get("rm_status") == "completed":
                return {"xml_id": xml_id, "status": "skipped", "reason": "already_analyzed"}
            if row.get("rm_status") == "failed" and attempt > MAX_ATTEMPTS:
                return {"xml_id": xml_id, "status": "skipped", "reason": "max_attempts"}
        async with self.semaphore:
            try:
                return await self.analyze_one(row, previous, attempt)
            except Exception as e:  # noqa: BLE001 — сбой одного XML не останавливает остальные
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

    # ------------------------------------------------------------- один XML

    async def analyze_one(self, row: Dict[str, Any], previous: Optional[Dict[str, Any]], attempt: int = 1) -> Dict[str, Any]:
        """Полный анализ одного XML и запись события.

        Args:
            row: Строка ``xml_meta.sql``.
            previous: Предыдущая версия (``xml_previous.sql``) или ``None``.
            attempt: Номер попытки.

        Returns:
            Dict[str, Any]: ``xml_id``, ``status``, ``pushed``, ``ai_analysis``.
        """
        xml_id = int(row["id"])
        raw = await self.archives.read(row["archive_url"], row["entry_name"])
        tag, xml_version, body = xml_events.parse_xml(raw)
        version = _int(row.get("eis_version"))
        version = xml_version if version is None else version

        prev_body = None
        if previous:
            try:
                prev_raw = await self.archives.read(previous["archive_url"], previous["entry_name"])
                _, _, prev_body = xml_events.parse_xml(prev_raw)
            except Exception as e:  # noqa: BLE001 — нет предыдущей версии: событие без изменений
                logger.warning(f"XML {xml_id}: предыдущая версия {previous.get('id')} не прочитана: {e}")
                previous = None

        facts = xml_events.extract_facts(tag, body)
        changes = xml_events.diff(xml_events.strip_ignored(prev_body), xml_events.strip_ignored(body)) if prev_body else []
        atts = xml_events.attachments(body)
        doc_changes = xml_events.document_changes(xml_events.attachments(prev_body) if prev_body else None, atts)
        all_files = await self._files(xml_id, previous, atts)
        urls = {a["url"] for a in atts}
        files = [f for f in all_files if f.get("xml_source_id") == xml_id or f.get("source_url") in urls]
        event = xml_events.classify(tag, version, previous is not None, changes, facts, doc_changes)

        link = None
        if row.get("risk_monitoring_contract_id") is None:
            link = await self._link_orphan(facts, row)

        rules = xml_events.rule_risks(event, changes, facts)
        llm = None
        if self.event_analyzer and self._needs_llm(event, changes):
            payload = xml_events.build_llm_payload(event, facts, changes, doc_changes, files)
            llm = await self.event_analyzer.analyze(payload, row.get("entry_name") or str(xml_id))

        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions,
                                           _int(previous["id"]) if previous else None, self.force, attempt,
                                           export_date=str(row.get("exact_date"))[:10] if row.get("exact_date") else None)
        ai = xml_events.build_xml_analysis(event=event, facts=facts, changes=changes, doc_changes=doc_changes,
                                           files=files, link=link, rule_risk_list=rules, llm=llm,
                                           previous={"id": previous["id"], "eis_version": _int(previous.get("eis_version"))} if previous else None,
                                           pipeline=pipeline, model=self.model if llm else None,
                                           all_files=all_files)
        response = {"xml_id": xml_id, "status": "done", "pushed": False, "ai_analysis": ai}
        if self.push_results:
            try:
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, xml_id, ai)
                response["pushed"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось записать ai_analysis XML {xml_id}: {e}")
                response["push_error"] = str(e)
        return response

    @staticmethod
    def _needs_llm(event: Dict[str, Any], changes: List[Dict[str, Any]]) -> bool:
        """LLM нужна, если событие не служебное: есть изменения, первая версия или важность выше низкой."""
        return bool(changes) or event.get("first_version") or event.get("importance") != "low"

    async def _files(self, xml_id: int, previous: Optional[Dict[str, Any]], atts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Файлы текущей и предыдущей версии XML (по ``xml_source_id`` и по ссылкам), с кратким результатом анализа."""
        ids = [xml_id] + ([int(previous["id"])] if previous else [])
        urls = [a["url"] for a in atts][:200]
        try:
            rows = await self._query("xml_files.sql", ids=ids, urls=urls)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Файлы XML {xml_id} не получены: {e}")
            return []
        out = []
        for r in rows:
            out.append({"file_id": _int(r.get("id")), "xml_source_id": _int(r.get("xml_source_id")),
                        "file_name": r.get("file_name"), "source_url": r.get("source_url"),
                        "detected_type": r.get("detected_type"), "resume": r.get("resume"),
                        "total_doc_risk": r.get("total_doc_risk")})
        return out

    async def _link_orphan(self, facts: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
        """Предложение привязки XML без закупки (лестница: номер закупки → номер контракта → ИКЗ → заказчик).

        Args:
            facts: Сведения XML.
            row: Строка ``xml_meta.sql`` (номера из колонок таблицы — дополнительный источник).

        Returns:
            Dict[str, Any]: ``method``, ``risk_monitoring_contract_id``, ``organisation_id``, ``ambiguous``,
            ``candidates``, ``customer_inn``, ``customer_kpp``. ``method = none`` — привязать не удалось.
        """
        link: Dict[str, Any] = {"method": "none", "risk_monitoring_contract_id": None, "organisation_id": None,
                                "ambiguous": False, "candidates": []}
        purchase_nums = [n for n in {facts.get("purchase_number"), row.get("purchase_number")} if n]
        contract_nums = [n for n in {facts.get("contract_reg_number"), row.get("contract_number")} if n]
        try:
            if purchase_nums:
                cards = await self._query("link_by_number.sql", nums=[str(n).strip() for n in purchase_nums])
                if cards:
                    link.update(method="purchase_number", risk_monitoring_contract_id=_int(cards[0]["id"]),
                                organisation_id=_int(cards[0].get("risk_monitoring_organisation_id")),
                                ambiguous=len(cards) > 1, candidates=[_int(c["id"]) for c in cards])
                    return link
            if contract_nums:
                rows = await self._query("link_by_contract_number.sql", nums=[str(n).strip() for n in contract_nums])
                if rows:
                    rows.sort(key=lambda r: -(_int(r.get("n")) or 0))
                    link.update(method="contract_number", risk_monitoring_contract_id=_int(rows[0]["risk_monitoring_contract_id"]),
                                ambiguous=len({r["risk_monitoring_contract_id"] for r in rows}) > 1,
                                candidates=[_int(r["risk_monitoring_contract_id"]) for r in rows])
                    return link
            parts = facts.get("ikz_parts")
            if parts:
                cards = await self._query("link_by_ikz.sql", nums=[parts["ikz"]])
                card, ambiguous = xml_events.pick_by_ikz(cards, facts.get("published_at") or facts.get("sign_date"))
                if card:
                    link.update(method="ikz", risk_monitoring_contract_id=_int(card["id"]),
                                organisation_id=_int(card.get("risk_monitoring_organisation_id")), ambiguous=ambiguous,
                                candidates=[_int(c["id"]) for c in cards])
                    return link
                link.update(customer_inn=parts["customer_inn"], customer_kpp=parts["customer_kpp"])
                orgs = await self._query("org_by_inn_kpp.sql", head=[parts["customer_inn"], parts["customer_kpp"]])
                org = xml_events.canonical_org(orgs, datetime.date.today().year)
                if org:
                    link.update(method="ikz_customer", organisation_id=_int(org["id"]))
        except Exception as e:  # noqa: BLE001 — привязка не обязательна для события
            logger.warning(f"Привязка XML не выполнена: {e}")
            link["error"] = str(e)
        return link

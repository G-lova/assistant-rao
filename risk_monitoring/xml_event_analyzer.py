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
                 use_llm: bool = True, concurrency: int = 3, repo: Any = None):
        """Создаёт компоненты: доступ к внешней БД, загрузчик архивов, LLM, API записи.

        ``repo`` (``configs.ai_repo.AIRepo``) нужен для срока хранения текстов: закрытие закупки/контракта
        сокращает срок ссылок её файлов. Без него события разбираются как прежде.
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
        self.storage_path = db.storage_path
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)
        self.archives = ArchiveCache(CloudStorageParser(http_manager, None))
        from risk_monitoring.llm_json import reasoning_llm

        # события разбирает модель для смысловых задач (RM_REASONING_MODEL), по умолчанию — get_llm()
        client, self.model = reasoning_llm(*get_llm())
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
            # 1) разбор XML, diff, правила — без модели
            prepared = await asyncio.gather(*(self._guarded(found[i], previous.get(i)) for i in ids if i in found))
            ctxs = [p for p in prepared if p.get("status") == "prepared"]
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
                return await self._prepare(row, previous, attempt)
            except Exception as e:  # noqa: BLE001 — сбой одного XML не останавливает остальные
                return await self._failure(xml_id, attempt, e)

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
            for batch in self.event_analyzer.batches(items):
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
        fz = risk_catalog.normalize_fz(row.get("fz")) or risk_catalog.normalize_fz(row.get("card_fz"))
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
        needs_llm, reason = xml_events.llm_decision(event, changes, rules)
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
        pipeline = passport.pipeline_block(STAGE, "completed", self.model, self.versions,
                                           _int(previous["id"]) if previous else None, self.force, ctx["attempt"],
                                           export_date=str(row.get("exact_date"))[:10] if row.get("exact_date") else None)
        pipeline["llm"] = {"used": llm is not None, "reason": ctx["llm_reason"]}
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
                await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, xml_id, ai)
                response["pushed"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось записать ai_analysis XML {xml_id}: {e}")
                response["push_error"] = str(e)
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

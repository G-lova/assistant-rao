"""Celery-задачи риск-мониторинга (очередь ``risk_monitoring``, отдельный воркер)."""
import asyncio
from typing import Any, Dict, List

from celery_app import celery_app
from configs.logger import get_logger

logger = get_logger(__name__)


def _summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Счётчики результатов по статусам."""
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.get("status", "unknown")] = counts.get(r.get("status", "unknown"), 0) + 1
    counts["pushed"] = sum(1 for r in results if r.get("pushed"))
    return counts


def _llm_summary(before: Dict[str, int], stats: Dict[str, Any]) -> Dict[str, Any]:
    """Сводка нагрузки на модель: запросы ``call_llm`` за задачу и статистика анализатора."""
    from risk_monitoring.llm_json import CALLS

    return {"requests": CALLS["requests"] - before["requests"], "failed": CALLS["failed"] - before["failed"], **(stats or {})}


PULSE_SECONDS = 60   # как часто задача пишет в журнал, что она жива и чем занята


async def _pulsed(coro: Any, name: str, task_id: str) -> Any:
    """Выполняет корутину задачи и раз в ``PULSE_SECONDS`` пишет в журнал «пульс».

    Пульс показывает, что задача жива, даже когда долгий запрос к модели не пишет ничего: сколько она идёт, сколько
    запросов к модели сделано, сколько выполняется сейчас, сколько сбоев и прерываний по пределу времени, и какие этапы
    идут прямо сейчас и как долго («Файл 2805 «ТЗ.docx»: анализ рисков: запрос к модели 340 с»; ``progress``).
    """
    import time

    from risk_monitoring import progress
    from risk_monitoring.llm_json import CALLS

    start, before = time.monotonic(), dict(CALLS)

    async def pulse() -> None:
        """Пишет пульс, пока задача не завершится."""
        while True:
            await asyncio.sleep(PULSE_SECONDS)
            logger.info(f"{name}[{task_id}]: идёт {time.monotonic() - start:.0f} с; запросов к модели "
                        f"{CALLS['requests'] - before.get('requests', 0)}, сейчас выполняется {CALLS.get('inflight', 0)}, "
                        f"сбоев {CALLS['failed'] - before.get('failed', 0)}, прервано по времени "
                        f"{CALLS.get('deadline', 0) - before.get('deadline', 0)}"
                        + ("; сейчас: " + "; ".join(progress.active()) if progress.active() else ""))

    beat = asyncio.ensure_future(pulse())
    try:
        return await coro
    finally:
        beat.cancel()


def _strip(results: List[Dict[str, Any]], include_analysis: bool) -> List[Dict[str, Any]]:
    """Убирает ``ai_analysis`` из ответа задачи (он уже записан во внешнюю БД), если не просили вернуть."""
    if include_analysis:
        return results
    return [{k: v for k, v in r.items() if k != "ai_analysis"} for r in results]


@celery_app.task(bind=True, name="rm_analyze_files_task", soft_time_limit=10500, time_limit=10800)
def rm_analyze_files_task(self, file_ids: List[int], environment: str, force: bool = False, recompute: bool = False,
                          push: bool = True, include_analysis: bool = False) -> Dict[str, Any]:
    """Анализ файлов ``risk_monitoring_files`` и запись AI-паспорта.

    Args:
        file_ids: id файлов.
        environment: Окружение внешней системы.
        force: Повторный анализ уже проанализированных (режим отладки).
        recompute: Игнорировать кэш LLM (режим отладки).
        push: Записывать результат во внешнюю БД.
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.

    Returns:
        Dict[str, Any]: ``summary`` (счётчики) и ``results`` по файлам.
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

    from risk_monitoring.llm_json import CALLS

    before = dict(CALLS)
    stats: Dict[str, Any] = {}

    async def run() -> List[Dict[str, Any]]:
        """Создаёт HTTP-менеджер и пул Postgres внутри event loop задачи и запускает анализ."""
        async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
            analyzer = AIFileAnalyzer(mgr, environment, repo, push_results=push, force=force, recompute=recompute)
            try:
                return await analyzer.analyze_ids(file_ids)
            finally:
                stats.update(analyzer.stats)

    results = asyncio.run(_pulsed(run(), self.name, self.request.id))
    summary = _summary(results)
    summary["llm"] = _llm_summary(before, stats)
    logger.info(f"Риск-мониторинг, файлы: {summary}")
    return {"summary": summary, "results": _strip(results, include_analysis)}


@celery_app.task(bind=True, name="rm_analyze_xml_task", soft_time_limit=10500, time_limit=10800)
def rm_analyze_xml_task(self, xml_ids: List[int], environment: str, force: bool = False, push: bool = True,
                        use_llm: bool = True, include_analysis: bool = False, analyze_files: bool = False,
                        recompute: bool = False) -> Dict[str, Any]:
    """Анализ XML ЕИС (``risk_monitoring_eis_xml_sources``) как событий и запись результата.

    При ``analyze_files=True`` сначала анализируются ещё не проанализированные файлы, указанные в XML
    (вложения, печатная форма) или привязанные к нему по ``xml_source_id``: так событие получает резюме и
    риски своих файлов. Результаты файлов — в ``files`` ответа.

    Args:
        xml_ids: id XML.
        environment: Окружение внешней системы.
        force: Повторный анализ уже проанализированных (режим отладки) — и XML, и файлов.
        push: Записывать результат во внешнюю БД.
        use_llm: Вызывать ``EventAnalyzer``.
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.
        analyze_files: Проанализировать файлы событий до самих событий.
        recompute: Игнорировать кэш LLM при анализе файлов (режим отладки).

    Returns:
        Dict[str, Any]: ``summary`` (счётчики) и ``results`` по XML; при ``analyze_files`` — ``files``
        (``summary``, ``results``).
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.llm_json import CALLS
    from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

    before = dict(CALLS)
    stats: Dict[str, Any] = {}
    files_out: Dict[str, Any] = {}

    async def run() -> List[Dict[str, Any]]:
        """Создаёт HTTP-менеджер внутри event loop задачи; при необходимости анализирует файлы, затем XML."""
        from configs.ai_repo import AIRepo

        async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
            # repo — внутреннее хранилище результатов (ai_rm_results); срок хранения текстов не трогаем
            analyzer = XmlEventAnalyzer(mgr, environment, push_results=push, force=force, use_llm=use_llm,
                                        results_repo=repo)
            if analyze_files:
                from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

                file_ids = await analyzer.pending_files(xml_ids, include_done=force)
                logger.info(f"Риск-мониторинг, XML {len(xml_ids)}: файлов к анализу {len(file_ids)}")
                if file_ids:
                    f_before = dict(CALLS)
                    fa = AIFileAnalyzer(mgr, environment, repo, push_results=push, force=force, recompute=recompute)
                    f_results = await fa.analyze_ids(file_ids)
                    analyzer.file_results = {int(r["file_id"]): r["ai_analysis"] for r in f_results
                                             if r.get("ai_analysis") and r.get("file_id") is not None}
                    files_out["summary"] = {**_summary(f_results), "llm": _llm_summary(f_before, fa.stats)}
                    files_out["results"] = _strip(f_results, include_analysis)
                else:
                    files_out["summary"] = {"total": 0}
                    files_out["results"] = []
            try:
                return await analyzer.analyze_ids(xml_ids)
            finally:
                stats.update(getattr(analyzer, "stats", {}) or {})

    results = asyncio.run(_pulsed(run(), self.name, self.request.id))
    summary = _summary(results)
    summary["llm"] = _llm_summary(before, stats)
    logger.info(f"Риск-мониторинг, XML: {summary}")
    out = {"summary": summary, "results": _strip(results, include_analysis)}
    if analyze_files:
        out["files"] = files_out
    return out


@celery_app.task(bind=True, name="rm_analyze_contracts_task", soft_time_limit=10500, time_limit=10800)
def rm_analyze_contracts_task(self, card_ids: List[int], environment: str, force: bool = False, push: bool = True,
                              use_llm: bool = True, include_analysis: bool = False,
                              analyze_documents: bool = True) -> Dict[str, Any]:
    """Анализ закупок/контрактов (``risk_monitoring_contracts``) по сохранённым результатам документов.

    Файлы и XML закупки без результата (ни во внешней БД, ни у нас) сначала анализируются (``analyze_documents``).

    Args:
        card_ids: id карточек.
        environment: Окружение внешней системы.
        force: Пересчитать при неизменных входных данных (режим отладки).
        push: Записывать результат во внешнюю БД.
        use_llm: Резюме пишет модель.
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.
        analyze_documents: Анализировать недостающие файлы и XML закупок.

    Returns:
        Dict[str, Any]: ``summary``, ``documents`` (анализ недостающих документов) и ``results`` по карточкам.
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.contract_analyzer import ContractAnalyzer
    from risk_monitoring.llm_json import CALLS

    before, stats = dict(CALLS), {}

    async def run() -> List[Dict[str, Any]]:
        """HTTP-менеджер и пул Postgres внутри event loop задачи."""
        async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
            analyzer = ContractAnalyzer(mgr, environment, repo, push_results=push, force=force, use_llm=use_llm,
                                        analyze_documents=analyze_documents)
            try:
                return await analyzer.analyze_ids(card_ids)
            finally:
                stats.update(analyzer.stats)
                documents.update(analyzer.documents)

    documents: Dict[str, Any] = {}
    results = asyncio.run(_pulsed(run(), self.name, self.request.id))
    summary = _summary(results)
    summary["llm"] = _llm_summary(before, stats)
    logger.info(f"Риск-мониторинг, закупки: {summary}; документы: {documents}")
    return {"summary": summary, "documents": documents, "results": _strip(results, include_analysis)}


@celery_app.task(bind=True, name="rm_analyze_orgs_task", soft_time_limit=3300, time_limit=3600)
def rm_analyze_orgs_task(self, org_ids: List[int], environment: str, roles: List[str] = ("customer", "supplier"),
                         period_months: int = None, push: bool = True, use_llm: bool = True,
                         include_analysis: bool = False) -> Dict[str, Any]:
    """Аналитика организаций как заказчика и поставщика.

    Args:
        org_ids: id ``risk_monitoring_organisations``.
        environment: Окружение внешней системы.
        roles: Секции ``customer`` / ``supplier``.
        period_months: Период, месяцев.
        push: Записывать во внешнюю БД (только при ``settings.ORG_PUSH_ENABLED``).
        use_llm: Резюме пишет модель.
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.

    Returns:
        Dict[str, Any]: ``summary`` и ``results`` по организациям.
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.llm_json import CALLS
    from risk_monitoring.org_analyzer import OrgAnalyzer

    before, stats = dict(CALLS), {}

    async def run() -> List[Dict[str, Any]]:
        """HTTP-менеджер и пул Postgres внутри event loop задачи."""
        async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
            analyzer = OrgAnalyzer(mgr, environment, repo, push_results=push, use_llm=use_llm, roles=tuple(roles),
                                   period_months=period_months)
            try:
                return await analyzer.analyze_ids(org_ids)
            finally:
                stats.update(analyzer.stats)

    results = asyncio.run(_pulsed(run(), self.name, self.request.id))
    summary = _summary(results)
    summary["llm"] = _llm_summary(before, stats)
    logger.info(f"Риск-мониторинг, организации: {summary}")
    return {"summary": summary, "results": _strip(results, include_analysis)}


@celery_app.task(bind=True, name="rm_typal_clauses_task", soft_time_limit=7000, time_limit=7200)
def rm_typal_clauses_task(self, environment: str = None, regions: List[str] = None, dates: List[str] = None,
                          xml_base64: str = None, read_files: bool = True) -> Dict[str, Any]:
    """Загрузка типовых положений о закупке (223-ФЗ) из ЕИС или из переданного XML.

    Без параметров (ежедневный запуск по расписанию): регионы — ``settings.TYPAL_REGIONS`` или регионы заказчиков
    на мониторинге окружения ``settings.TYPAL_REGIONS_ENVIRONMENT``; даты — последние
    ``settings.TYPAL_LOOKBACK_DAYS`` дней. Уже сохранённые версии перезаписываются без повторного чтения файлов.

    Args:
        environment: Окружение (для поиска регионов).
        regions: Коды регионов ФНС.
        dates: Даты ``ГГГГ-ММ-ДД``.
        xml_base64: XML ``typalOrderClause`` в base64 (ручная загрузка) — тогда ЕИС не запрашивается.
        read_files: Скачивать и читать файлы положений.

    Returns:
        Dict[str, Any]: ``loaded`` (версии), ``stats``, ``regions``, ``dates``.
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs import settings

    if not settings.TYPAL_LOAD_ENABLED and not xml_base64:
        return {"skipped": "TYPAL_LOAD_ENABLED = False"}
    import base64

    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.typal_loader import TypalLoader, lookback_dates

    out: Dict[str, Any] = {"loaded": [], "errors": []}

    async def run() -> None:
        """HTTP-менеджер и пул Postgres внутри event loop задачи."""
        async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
            loader = TypalLoader(mgr, repo, environment or settings.TYPAL_REGIONS_ENVIRONMENT, read_files=read_files)
            try:
                if xml_base64:
                    out["loaded"] += await loader.load_xml(base64.b64decode(xml_base64), source="upload")
                    return
                regs = regions or await loader.regions()
                days = dates or lookback_dates(settings.TYPAL_LOOKBACK_DAYS)
                out.update(regions=regs, dates=days)
                for reg in regs:
                    for d in days:
                        try:
                            out["loaded"] += await loader.load_region(reg, d)
                        except Exception as e:  # noqa: BLE001 — регион/дата не останавливают остальные
                            logger.warning(f"Типовые положения {reg} за {d}: {type(e).__name__}: {e}")
                            out["errors"].append({"region": reg, "date": d, "error": f"{type(e).__name__}: {e}"[:300]})
            finally:
                out["stats"] = loader.stats

    asyncio.run(_pulsed(run(), self.name, self.request.id))
    logger.info(f"Типовые положения: {out.get('stats')}, версий {len(out['loaded'])}")
    return out


async def _daily_run(environment: str, export_date: str, force: bool, push: bool, use_llm: bool,
                     stages: List[str] = None) -> Dict[str, Any]:
    """Прогон за дату с защитой от повтора: готовая сводка возвращается без пересчёта (кроме ``force``),
    идущий прогон второй раз не запускается."""
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring import daily

    d = daily.parse_date(export_date).isoformat()
    async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
        env = environment.lower()
        stored = (await repo.get_rm_results(env, daily.KIND, [daily.run_key(d)])).get(daily.run_key(d))
        if not force and daily.completed(stored):
            logger.info(f"Ежедневный прогон {d} ({env}) уже выполнен — повтор не нужен")
            return {"status": "already_done", "date": d, "summary": stored["ai_analysis"]}
        # и с force: два прогона одной даты параллельно делят документы и перезаписывают сводку друг друга
        # (21.10.2026: прогоны 21:58 и 22:16 шли одновременно)
        if daily.running_fresh(stored):
            logger.info(f"Ежедневный прогон {d} ({env}) уже идёт — второй не запускается")
            return {"status": "already_running", "date": d}
        run = daily.DailyRun(mgr, environment, repo, push_results=push, use_llm=use_llm, stages=stages)
        summary = await run.run(d)
        return {"status": "done", "date": d, "summary": summary}


def continuation_delay(status: str, chain: int, cfg: Any = None) -> Any:
    """Через сколько секунд продолжить прогон за ту же дату (или ``None`` — не продолжать).

    Args:
        status: Итог запуска: ``partial`` (не хватило времени), ``failed`` (упал), иначе — готово.
        chain: Сколько продолжений уже было подряд.
        cfg: Настройки (по умолчанию ``configs.settings``).

    Returns:
        Optional[int]: Задержка, с.
    """
    if cfg is None:
        from configs import settings as cfg
    if not cfg.DAILY_CONTINUE_ENABLED or chain >= cfg.DAILY_CONTINUE_MAX_ATTEMPTS:
        return None
    if status == "partial":
        return cfg.DAILY_CONTINUE_DELAY_SEC
    if status == "failed":
        return cfg.DAILY_RETRY_FAILED_DELAY_SEC
    return None


def _continue(environment: str, export_date: str, status: str, chain: int, push: Any, use_llm: bool,
              stages: Any) -> None:
    """Ставит продолжение прогона за ту же дату, если он не завершён (``partial`` / ``failed``): ежедневная сводка
    должна быть готова в тот же день, без ручного запуска."""
    delay = continuation_delay(status, chain)
    if delay is None:
        if status in ("partial", "failed"):
            logger.warning(f"Ежедневный прогон {export_date}: {status}, продолжений подряд {chain} — "
                           f"автоматически больше не продолжается, нужен ручной запуск")
        return
    rm_daily_task.apply_async(
        kwargs={"environment": environment, "export_date": export_date, "push": push, "use_llm": use_llm,
                "stages": stages, "chain": chain + 1},
        countdown=delay, queue="risk_monitoring")
    logger.info(f"Ежедневный прогон {export_date}: {status} — продолжение №{chain + 1} через {delay} с")


def _outcome(out: Any) -> str:
    """Итог прогона по результату ``_daily_run``: ``completed`` / ``partial`` / ``failed`` / ``skip``."""
    if not isinstance(out, dict):
        return "skip"
    if out.get("status") == "done":
        return ((out.get("summary") or {}).get("pipeline") or {}).get("status") or "completed"
    return "skip"   # already_done / already_running


@celery_app.task(bind=True, name="rm_daily_task", soft_time_limit=13500, time_limit=14100)
def rm_daily_task(self, environment: str, export_date: str, force: bool = False, push: bool = None,
                  use_llm: bool = True, stages: List[str] = None, chain: int = 0) -> Dict[str, Any]:
    """Ежедневный прогон за дату выгрузки (S0–S4) и сводка (``risk_monitoring.daily``).

    Args:
        environment: Окружение внешней системы.
        export_date: Дата выгрузки ЕИС ``ГГГГ-ММ-ДД``.
        force: Пересобрать готовую сводку (режим отладки; документы всё равно повторно не анализируются).
        push: Записывать результаты этапов во внешнюю БД (по умолчанию ``settings.DAILY_PUSH``).
        use_llm: Резюме пишет модель.
        stages: Этапы (по умолчанию все).
        chain: Номер автоматического продолжения (``partial`` / ``failed`` доделываются сами, см. :func:`_continue`).

    Returns:
        Dict[str, Any]: ``status`` (``done`` / ``already_done`` / ``already_running``), ``date``, ``summary``.
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs import settings

    push = settings.DAILY_PUSH if push is None else push
    if force and (self.request.delivery_info or {}).get("redelivered"):
        # сообщение задачи, оборванной пересборкой, брокер вернул через несколько часов: его force относится к старому
        # запросу — готовую с тех пор сводку не пересобираем (21.10.2026: лишние прогоны за 2026-07-03)
        logger.info(f"rm_daily_task[{self.request.id}]: повторная доставка — force не применяется")
        force = False
    try:
        out = asyncio.run(_pulsed(_daily_run(environment, export_date, force, push, use_llm, stages),
                                  self.name, self.request.id))
    except BaseException as e:
        # прогон упал или снят по лимиту времени (failed уже сохранён в DailyRun.run) — повтор позже
        if isinstance(e, Exception) or type(e).__name__ == "SoftTimeLimitExceeded":
            _continue(environment, export_date, "failed", chain, push, use_llm, stages)
        raise
    _continue(environment, export_date, _outcome(out), chain, push, use_llm, stages)
    return out


@celery_app.task(bind=True, name="rm_daily_watchdog_task", soft_time_limit=13500, time_limit=14100)
def rm_daily_watchdog_task(self) -> Dict[str, Any]:
    """Страховка по расписанию: если Laravel не вызвал прогон за последнюю дату выгрузки, запускает его сам.

    Окружение — ``settings.DAILY_ENVIRONMENT``; дата — последняя ``exact_date`` архивов ЕИС не позже сегодня.
    Готовую или идущую сводку не трогает (``_daily_run``).

    Returns:
        Dict[str, Any]: Результат прогона или причина пропуска.
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result
    from configs import settings

    if not settings.DAILY_WATCHDOG_ENABLED:
        return {"skipped": "DAILY_WATCHDOG_ENABLED = False"}

    async def run() -> Dict[str, Any]:
        """Последняя дата выгрузки и прогон за неё."""
        from datetime import date

        from configs.http_client_manager import HTTPClientManager
        from risk_monitoring import daily

        async with HTTPClientManager(timeout=60.0) as mgr:
            probe = daily.DailyRun(mgr, settings.DAILY_ENVIRONMENT, None)
            rows = await probe._query("latest_export_date.sql", head=[date.today().isoformat()])
        last = rows[0].get("exact_date") if rows else None
        if not last:
            return {"skipped": "нет выгрузок ЕИС"}
        logger.info(f"Страховка ежедневного прогона: последняя выгрузка {str(last)[:10]} ({settings.DAILY_ENVIRONMENT})")
        res = await _daily_run(settings.DAILY_ENVIRONMENT, str(last)[:10], False, settings.DAILY_PUSH, True)
        res["_date"] = str(last)[:10]
        return res

    # сбой до или во время прогона: дата и отметка failed сохранены — продолжение поставит rm_daily_resume_task
    out = asyncio.run(_pulsed(run(), self.name, self.request.id))
    if isinstance(out, dict) and out.get("_date"):
        _continue(settings.DAILY_ENVIRONMENT, out.pop("_date"), _outcome(out), 0, settings.DAILY_PUSH, True, None)
    return {k: v for k, v in out.items() if k != "summary"} if isinstance(out, dict) else out


@celery_app.task(bind=True, name="rm_daily_resume_task", soft_time_limit=300, time_limit=360)
def rm_daily_resume_task(self) -> Dict[str, Any]:
    """Страховка по расписанию (каждые ``DAILY_RESUME_EVERY_MIN`` минут): незавершённые ежедневные прогоны
    (``partial``, ``failed``, оборванный ``running``) за последние ``DAILY_RESUME_LOOKBACK_HOURS`` часов ставятся
    на продолжение — если продолжение после самого прогона не поставилось (перезапуск контейнера, потерянное
    сообщение).

    Не трогает: идущий прогон (свежий пульс), только что обновлённый (продолжение уже стоит в очереди), прогон,
    исчерпавший ``DAILY_CONTINUE_MAX_ATTEMPTS`` запусков.

    Returns:
        Dict[str, Any]: ``queued`` — даты, поставленные на продолжение; ``skipped`` — пропущенные с причиной.
    """
    from configs import settings

    if not settings.DAILY_CONTINUE_ENABLED:
        return {"skipped": "DAILY_CONTINUE_ENABLED = False"}

    async def run() -> Dict[str, Any]:
        """Незавершённые прогоны и решение по каждому."""
        from datetime import datetime, timezone

        from configs.ai_repo import AIRepo
        from risk_monitoring import daily

        env = settings.DAILY_ENVIRONMENT.lower()
        async with AIRepo() as repo:
            rows = await repo.list_rm_unfinished(env, daily.KIND, settings.DAILY_RESUME_LOOKBACK_HOURS)
        queued, skipped = [], []
        now = datetime.now(timezone.utc)
        for r in rows:
            ai = r["ai_analysis"] or {}
            d = ai.get("date") or daily.key_date(r["external_id"])
            pipe = ai.get("pipeline") or {}
            if r["status"] == "running" and daily.running_fresh({"ai_analysis": ai}):
                skipped.append({"date": d, "reason": "идёт"})
                continue
            age = (now - r["analyzed_at"]).total_seconds() if r.get("analyzed_at") else None
            if r["status"] != "running" and age is not None and age < settings.DAILY_RETRY_FAILED_DELAY_SEC + 300:
                skipped.append({"date": d, "reason": "продолжение уже запланировано"})
                continue
            if int(pipe.get("attempt") or 0) >= settings.DAILY_CONTINUE_MAX_ATTEMPTS:
                skipped.append({"date": d, "reason": f"запусков {pipe.get('attempt')} — нужен ручной запуск"})
                continue
            rm_daily_task.apply_async(kwargs={"environment": settings.DAILY_ENVIRONMENT, "export_date": d,
                                              "push": settings.DAILY_PUSH}, queue="risk_monitoring")
            queued.append(d)
        if queued:
            logger.info(f"Незавершённые ежедневные прогоны поставлены на продолжение: {queued}")
        return {"queued": queued, "skipped": skipped}

    return asyncio.run(run())

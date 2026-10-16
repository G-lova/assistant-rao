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

    results = asyncio.run(run())
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

    results = asyncio.run(run())
    summary = _summary(results)
    summary["llm"] = _llm_summary(before, stats)
    logger.info(f"Риск-мониторинг, XML: {summary}")
    out = {"summary": summary, "results": _strip(results, include_analysis)}
    if analyze_files:
        out["files"] = files_out
    return out


@celery_app.task(bind=True, name="rm_analyze_contracts_task", soft_time_limit=3300, time_limit=3600)
def rm_analyze_contracts_task(self, card_ids: List[int], environment: str, force: bool = False, push: bool = True,
                              use_llm: bool = True, include_analysis: bool = False) -> Dict[str, Any]:
    """Анализ закупок/контрактов (``risk_monitoring_contracts``) по сохранённым результатам документов.

    Args:
        card_ids: id карточек.
        environment: Окружение внешней системы.
        force: Пересчитать при неизменных входных данных (режим отладки).
        push: Записывать результат во внешнюю БД.
        use_llm: Резюме пишет модель.
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.

    Returns:
        Dict[str, Any]: ``summary`` и ``results`` по карточкам.
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
            analyzer = ContractAnalyzer(mgr, environment, repo, push_results=push, force=force, use_llm=use_llm)
            try:
                return await analyzer.analyze_ids(card_ids)
            finally:
                stats.update(analyzer.stats)

    results = asyncio.run(run())
    summary = _summary(results)
    summary["llm"] = _llm_summary(before, stats)
    logger.info(f"Риск-мониторинг, закупки: {summary}")
    return {"summary": summary, "results": _strip(results, include_analysis)}


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

    results = asyncio.run(run())
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
    from risk_monitoring import settings

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

    asyncio.run(run())
    logger.info(f"Типовые положения: {out.get('stats')}, версий {len(out['loaded'])}")
    return out

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
        async with HTTPClientManager(timeout=180.0) as mgr:
            analyzer = XmlEventAnalyzer(mgr, environment, push_results=push, force=force, use_llm=use_llm)
            if analyze_files:
                from configs.ai_repo import AIRepo
                from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

                file_ids = await analyzer.pending_files(xml_ids, include_done=force)
                logger.info(f"Риск-мониторинг, XML {len(xml_ids)}: файлов к анализу {len(file_ids)}")
                if file_ids:
                    f_before = dict(CALLS)
                    async with AIRepo() as repo:
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

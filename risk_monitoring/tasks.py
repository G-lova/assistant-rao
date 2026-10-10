"""Celery-задачи риск-мониторинга (очередь ``risk_monitoring``, отдельный воркер)."""
import asyncio
from typing import Any, Dict, List

from celery_app import celery_app
from configs.logger import get_logger

logger = get_logger(__name__)


def _summary(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Счётчики результатов по статусам."""
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.get("status", "unknown")] = counts.get(r.get("status", "unknown"), 0) + 1
    counts["pushed"] = sum(1 for r in results if r.get("pushed"))
    return counts


def _strip(results: List[Dict[str, Any]], include_analysis: bool) -> List[Dict[str, Any]]:
    """Убирает ``ai_analysis`` из ответа задачи (он уже записан во внешнюю БД), если не просили вернуть."""
    if include_analysis:
        return results
    return [{k: v for k, v in r.items() if k != "ai_analysis"} for r in results]


@celery_app.task(bind=True, name="rm_analyze_files_task", soft_time_limit=3300, time_limit=3600)
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
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

    async def run() -> List[Dict[str, Any]]:
        """Создаёт HTTP-менеджер и пул Postgres внутри event loop задачи и запускает анализ."""
        async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
            analyzer = AIFileAnalyzer(mgr, environment, repo, push_results=push, force=force, recompute=recompute)
            return await analyzer.analyze_ids(file_ids)

    results = asyncio.run(run())
    return {"summary": _summary(results), "results": _strip(results, include_analysis)}


@celery_app.task(bind=True, name="rm_analyze_xml_task", soft_time_limit=3300, time_limit=3600)
def rm_analyze_xml_task(self, xml_ids: List[int], environment: str, force: bool = False, push: bool = True,
                        use_llm: bool = True, include_analysis: bool = False) -> Dict[str, Any]:
    """Анализ XML ЕИС (``risk_monitoring_eis_xml_sources``) как событий и запись результата.

    Args:
        xml_ids: id XML.
        environment: Окружение внешней системы.
        force: Повторный анализ уже проанализированных (режим отладки).
        push: Записывать результат во внешнюю БД.
        use_llm: Вызывать ``EventAnalyzer``.
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.

    Returns:
        Dict[str, Any]: ``summary`` (счётчики) и ``results`` по XML.
    """
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

    async def run() -> List[Dict[str, Any]]:
        """Создаёт HTTP-менеджер внутри event loop задачи и запускает анализ."""
        async with HTTPClientManager(timeout=180.0) as mgr:
            analyzer = XmlEventAnalyzer(mgr, environment, push_results=push, force=force, use_llm=use_llm)
            return await analyzer.analyze_ids(xml_ids)

    results = asyncio.run(run())
    return {"summary": _summary(results), "results": _strip(results, include_analysis)}

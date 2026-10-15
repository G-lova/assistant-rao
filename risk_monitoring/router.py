"""Эндпоинты риск-мониторинга (новые; существующие эндпоинты ``main.py`` не меняются).

* ``POST /risk-monitoring/files/analysis`` — анализ файлов по id → AI-паспорт документа;
* ``POST /risk-monitoring/xml/analysis`` — анализ XML ЕИС по id → событие;
* статус задачи — существующий ``GET /task/{task_id}``.

Окружение внешней системы — заголовок ``X-API-Database`` (как в остальных эндпоинтах).
Повторный анализ уже проанализированных документов (``force``, ``recompute``) разрешён только
при ``RISK_MONITORING_DEBUG=true``.
"""
from typing import List

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field, validator

from configs.config import Config

router = APIRouter(prefix="/risk-monitoring", tags=["risk-monitoring"])

MAX_IDS = 500


class AnalysisRequest(BaseModel):
    """Запрос на анализ по списку id.

    Attributes:
        ids: id записей (``risk_monitoring_files`` или ``risk_monitoring_eis_xml_sources``).
        force: Повторно проанализировать уже проанализированные (только режим отладки).
        recompute: Игнорировать кэш LLM (только режим отладки, только для файлов).
        push: Записывать результат во внешнюю БД (``false`` — пробный прогон, результат в задаче).
        include_analysis: Вернуть ``ai_analysis`` в результате задачи.
    """

    ids: List[int] = Field(..., min_items=1)
    force: bool = False
    recompute: bool = False
    push: bool = True
    include_analysis: bool = False

    @validator("ids")
    def _check_ids(cls, v: List[int]) -> List[int]:
        """Не больше ``MAX_IDS``, только положительные, без повторов."""
        v = list(dict.fromkeys(int(i) for i in v))
        if len(v) > MAX_IDS:
            raise ValueError(f"не больше {MAX_IDS} id за запрос")
        if any(i <= 0 for i in v):
            raise ValueError("id должны быть положительными")
        return v


def _check_debug(req: AnalysisRequest) -> None:
    """Запрещает повторный анализ вне режима отладки."""
    if (req.force or req.recompute) and not Config.risk_monitoring_debug():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Повторный анализ (force/recompute) доступен только при RISK_MONITORING_DEBUG=true")


@router.post("/files/analysis", status_code=status.HTTP_202_ACCEPTED)
async def analyze_files(req: AnalysisRequest, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Ставит в очередь анализ файлов ``risk_monitoring_files``.

    Уже проанализированные файлы (``ai_analysis.pipeline.status = completed``) пропускаются.

    Args:
        req: Список id и режимы.
        x_api_database: Окружение внешней системы.

    Returns:
        dict: ``task_id`` (статус — ``GET /task/{task_id}``), ``count``.
    """
    _check_debug(req)
    from risk_monitoring.tasks import rm_analyze_files_task

    task = rm_analyze_files_task.apply_async(
        args=[req.ids, x_api_database],
        kwargs={"force": req.force, "recompute": req.recompute, "push": req.push,
                "include_analysis": req.include_analysis},
        queue="risk_monitoring")
    return {"task_id": task.id, "count": len(req.ids), "status": "queued"}


@router.post("/xml/analysis", status_code=status.HTTP_202_ACCEPTED)
async def analyze_xml(req: AnalysisRequest, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Ставит в очередь анализ XML ЕИС (``risk_monitoring_eis_xml_sources``) как событий.

    Уже проанализированные XML пропускаются.

    Args:
        req: Список id и режимы (``recompute`` для XML не используется).
        x_api_database: Окружение внешней системы.

    Returns:
        dict: ``task_id``, ``count``.
    """
    _check_debug(req)
    from risk_monitoring.tasks import rm_analyze_xml_task

    task = rm_analyze_xml_task.apply_async(
        args=[req.ids, x_api_database],
        kwargs={"force": req.force, "push": req.push, "include_analysis": req.include_analysis},
        queue="risk_monitoring")
    return {"task_id": task.id, "count": len(req.ids), "status": "queued"}

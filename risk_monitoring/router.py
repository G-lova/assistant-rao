"""Эндпоинты риск-мониторинга (новые; существующие эндпоинты ``main.py`` не меняются).

* ``POST /risk-monitoring/files/analysis`` — анализ файлов по id → AI-паспорт документа;
* ``POST /risk-monitoring/xml/analysis`` — анализ XML ЕИС по id → событие;
* ``POST /risk-monitoring/contracts/analysis`` — анализ закупки/контракта по id карточки → паспорт, комплект, риски, балл;
* ``POST /risk-monitoring/organisations/analysis`` — аналитика организации как заказчика и поставщика
  (принимается и написание ``/organizations/analysis``);
* ``POST /risk-monitoring/typal-clauses/load`` и ``/typal-clauses/xml`` — загрузка типовых положений (223-ФЗ);
* ``GET /risk-monitoring/typal-clauses/customer/{inn}`` — типовое положение заказчика;
* ``POST /risk-monitoring/daily`` — ежедневный прогон за дату выгрузки и сводка; ``GET /risk-monitoring/daily/{date}`` —
  готовая сводка;
* статус задачи — существующий ``GET /task/{task_id}``.

Окружение внешней системы — заголовок ``X-API-Database`` (как в остальных эндпоинтах).
Повторный анализ уже проанализированных документов (``force``, ``recompute``) разрешён только
в режиме отладки (``configs.settings.RISK_MONITORING_DEBUG``).
"""
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
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
        analyze_files: Только для XML — сначала проанализировать ещё не проанализированные файлы,
            указанные в XML (вложения, печатная форма) или привязанные к нему.
    """

    ids: List[int] = Field(..., min_items=1)
    force: bool = False
    recompute: bool = False
    push: bool = True
    include_analysis: bool = False
    analyze_files: bool = False

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
                            detail="Повторный анализ (force/recompute) доступен только в режиме отладки (RISK_MONITORING_DEBUG)")


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
        req: Список id и режимы; ``analyze_files`` — сначала файлы событий (``recompute`` — для них).
        x_api_database: Окружение внешней системы.

    Returns:
        dict: ``task_id``, ``count``.
    """
    _check_debug(req)
    from risk_monitoring.tasks import rm_analyze_xml_task

    task = rm_analyze_xml_task.apply_async(
        args=[req.ids, x_api_database],
        kwargs={"force": req.force, "push": req.push, "include_analysis": req.include_analysis,
                "analyze_files": req.analyze_files, "recompute": req.recompute},
        queue="risk_monitoring")
    return {"task_id": task.id, "count": len(req.ids), "status": "queued"}


class PackageRequest(AnalysisRequest):
    """Запрос на анализ закупок/контрактов или организаций.

    Attributes:
        use_llm: Резюме пишет модель (иначе — шаблон).
        analyze_documents: Только для закупок — проанализировать файлы и XML, у которых нет результата ни во внешней
            БД, ни у нас (``false`` — только сборка по готовым результатам).
        roles: Только для организаций — секции ``customer`` и/или ``supplier``.
        period_months: Только для организаций — период аналитики, месяцев (по умолчанию 12).
    """

    use_llm: bool = True
    analyze_documents: bool = True
    roles: List[str] = Field(default_factory=lambda: ["customer", "supplier"])
    period_months: Optional[int] = Field(default=None, ge=1, le=60)

    @validator("roles")
    def _check_roles(cls, v: List[str]) -> List[str]:
        """Допустимые роли: ``customer``, ``supplier``."""
        bad = [r for r in v if r not in ("customer", "supplier")]
        if bad or not v:
            raise ValueError("roles: допустимо customer, supplier")
        return list(dict.fromkeys(v))


@router.post("/contracts/analysis", status_code=status.HTTP_202_ACCEPTED)
async def analyze_contracts(req: PackageRequest, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Ставит в очередь анализ закупок/контрактов (``risk_monitoring_contracts``): паспорт, комплект,
    согласованность, сводные риски файлов, событий, правил и реестра 19.28, балл и резюме.

    Документы повторно не анализируются — берутся сохранённые результаты (внешняя БД, затем наше хранилище); файлы и
    XML без результата сначала анализируются (``analyze_documents``). Закупка пересчитывается, если изменились
    входные данные; ``force`` (только в режиме отладки) — пересчитать в любом случае.

    Args:
        req: id карточек и режимы (``push``, ``use_llm``, ``include_analysis``).
        x_api_database: Окружение внешней системы.

    Returns:
        dict: ``task_id``, ``count``.
    """
    _check_debug(req)
    from risk_monitoring.tasks import rm_analyze_contracts_task

    task = rm_analyze_contracts_task.apply_async(
        args=[req.ids, x_api_database],
        kwargs={"force": req.force, "push": req.push, "use_llm": req.use_llm, "include_analysis": req.include_analysis,
                "analyze_documents": req.analyze_documents},
        queue="risk_monitoring")
    return {"task_id": task.id, "count": len(req.ids), "status": "queued"}


@router.post("/organisations/analysis", status_code=status.HTTP_202_ACCEPTED)
@router.post("/organizations/analysis", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
async def analyze_organisations(req: PackageRequest, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Ставит в очередь аналитику организаций (``risk_monitoring_organisations``) как заказчика и поставщика.

    Результат — секции ``customer`` / ``supplier`` (метрики за период, риски, балл, резюме). Результат пишется в
    ``risk_monitoring_organisations.ai_analysis`` (``settings.ORG_PUSH_ENABLED``), хранится у нас и возвращается
    в задаче (``include_analysis``).

    Args:
        req: id организаций, ``roles``, ``period_months`` и режимы.
        x_api_database: Окружение внешней системы.

    Returns:
        dict: ``task_id``, ``count``.
    """
    from risk_monitoring.tasks import rm_analyze_orgs_task

    task = rm_analyze_orgs_task.apply_async(
        args=[req.ids, x_api_database],
        kwargs={"roles": req.roles, "period_months": req.period_months, "push": req.push, "use_llm": req.use_llm,
                "include_analysis": req.include_analysis},
        queue="risk_monitoring")
    return {"task_id": task.id, "count": len(req.ids), "status": "queued"}


class TypalLoadRequest(BaseModel):
    """Загрузка типовых положений из ЕИС.

    Attributes:
        regions: Коды регионов ФНС (две цифры); пусто — ``settings.TYPAL_REGIONS`` или регионы заказчиков
            на мониторинге.
        dates: Даты выгрузки ``ГГГГ-ММ-ДД``; пусто — последние ``settings.TYPAL_LOOKBACK_DAYS`` дней.
        read_files: Скачивать и читать файлы положений.
    """

    regions: List[str] = Field(default_factory=list)
    dates: List[str] = Field(default_factory=list, max_items=93)
    read_files: bool = True

    @validator("regions", each_item=True)
    def _check_region(cls, v: str) -> str:
        """Код региона — 1–2 цифры (ФНС)."""
        v = str(v).strip()
        if not v.isdigit() or len(v) > 2:
            raise ValueError("код региона ФНС — две цифры, например 34")
        return v.zfill(2)

    @validator("dates", each_item=True)
    def _check_date(cls, v: str) -> str:
        """Дата ``ГГГГ-ММ-ДД``."""
        from datetime import date

        return date.fromisoformat(str(v)).isoformat()


@router.post("/typal-clauses/load", status_code=status.HTTP_202_ACCEPTED)
async def load_typal_clauses(req: TypalLoadRequest, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Ставит в очередь загрузку типовых положений о закупке (223-ФЗ, ``typalOrderClause``) из ЕИС.

    Args:
        req: Регионы, даты, чтение файлов.
        x_api_database: Окружение (для определения регионов заказчиков на мониторинге).

    Returns:
        dict: ``task_id``.
    """
    from risk_monitoring.tasks import rm_typal_clauses_task

    task = rm_typal_clauses_task.apply_async(
        kwargs={"environment": x_api_database, "regions": req.regions or None, "dates": req.dates or None,
                "read_files": req.read_files}, queue="risk_monitoring")
    return {"task_id": task.id, "status": "queued"}


@router.post("/typal-clauses/xml", status_code=status.HTTP_202_ACCEPTED)
async def upload_typal_clause(request: Request, read_files: bool = True):
    """Ставит в очередь загрузку типового положения из XML ``typalOrderClause`` (тело запроса — сам XML).

    Args:
        request: Запрос; тело — XML (до 30 МБ).
        read_files: Скачивать и читать файлы положения по ссылкам из XML.

    Returns:
        dict: ``task_id``, реестровый номер и версия.

    Raises:
        HTTPException: 400 — не ``typalOrderClause``; 413 — больше 30 МБ.
    """
    import base64

    from risk_monitoring import typal_clauses
    from risk_monitoring.tasks import rm_typal_clauses_task

    raw = await request.body()
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="XML больше 30 МБ")
    try:
        clauses = typal_clauses.parse_typal_xml(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    task = rm_typal_clauses_task.apply_async(
        kwargs={"xml_base64": base64.b64encode(raw).decode("ascii"), "read_files": read_files}, queue="risk_monitoring")
    return {"task_id": task.id, "status": "queued",
            "versions": [{"registration_number": c["registration_number"], "version": c["version"],
                          "customers": len(c["customers"]), "files": len(c["files"])} for c in clauses]}


@router.get("/typal-clauses/customer/{inn}")
async def customer_typal_clause(inn: str, kpp: Optional[str] = None, on: Optional[str] = None):
    """Типовое положение, обязательное для заказчика: действующая на дату редакция и файл-положение по типу учреждения.

    Args:
        inn: ИНН заказчика.
        kpp: КПП (необязательно).
        on: Дата ``ГГГГ-ММ-ДД`` (по умолчанию сегодня).

    Returns:
        dict: Реквизиты редакции, ``agency_kind`` заказчика, ``files``, ``position``.

    Raises:
        HTTPException: 404 — заказчика нет ни в одном загруженном типовом положении.
    """
    from configs.ai_repo import AIRepo
    from risk_monitoring.typal_clauses import TypalRepo

    if not inn.isdigit() or len(inn) not in (10, 12):
        raise HTTPException(status_code=400, detail="ИНН — 10 или 12 цифр")
    async with AIRepo() as repo:
        found = await TypalRepo(repo.pool).customer_clause(inn, kpp, on)
    if not found:
        raise HTTPException(status_code=404, detail="Заказчик не найден в загруженных типовых положениях")
    return found


class DailyRequest(BaseModel):
    """Ежедневный прогон за дату выгрузки ЕИС.

    Attributes:
        date: Дата выгрузки ``ГГГГ-ММ-ДД`` (``risk_monitoring_eis_archives.exact_date``).
        stages: Этапы (``files``, ``xml``, ``contracts``, ``customers``, ``summary``); пусто — все.
        force: Пересобрать готовую сводку (только в режиме отладки; документы повторно не анализируются).
        push: Записывать результаты этапов во внешнюю БД; ``None`` — ``settings.DAILY_PUSH``.
        use_llm: Резюме пишет модель.
    """

    date: str
    stages: List[str] = Field(default_factory=list)
    force: bool = False
    push: Optional[bool] = None
    use_llm: bool = True

    @validator("date")
    def _check_date(cls, v: str) -> str:
        """Дата ``ГГГГ-ММ-ДД``."""
        from datetime import date

        return date.fromisoformat(str(v)[:10]).isoformat()

    @validator("stages", each_item=True)
    def _check_stage(cls, v: str) -> str:
        """Этап из списка ``daily.STAGES``."""
        from risk_monitoring.daily import STAGES

        if v not in STAGES:
            raise ValueError(f"этап — один из {', '.join(STAGES)}")
        return v


@router.post("/daily", status_code=status.HTTP_202_ACCEPTED)
async def run_daily(req: DailyRequest, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Ставит в очередь ежедневный прогон за дату выгрузки: файлы и события XML за дату, затронутые закупки,
    заказчики на мониторинге, сводка (``risk_monitoring.daily``).

    Вызывается Laravel-командой после загрузки ЕИС; повторный вызов за ту же дату вернёт готовую сводку, а идущий
    прогон второй раз не запустит. Если вызова не было, прогон запускает расписание (``rm_daily_watchdog_task``).

    Args:
        req: Дата, этапы, режимы.
        x_api_database: Окружение внешней системы.

    Returns:
        dict: ``task_id``, ``date``.
    """
    if req.force and not Config.risk_monitoring_debug():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Пересборка готовой сводки (force) доступна только в режиме отладки (RISK_MONITORING_DEBUG)")
    from risk_monitoring.tasks import rm_daily_task

    task = rm_daily_task.apply_async(
        args=[x_api_database, req.date],
        kwargs={"force": req.force, "push": req.push, "use_llm": req.use_llm, "stages": req.stages or None},
        queue="risk_monitoring")
    return {"task_id": task.id, "date": req.date, "status": "queued"}


@router.get("/daily/{export_date}")
async def get_daily(export_date: str, x_api_database: str = Header(default="dev", alias="X-API-Database")):
    """Сводка ежедневного прогона за дату выгрузки (или отметка, что прогон идёт).

    Args:
        export_date: Дата ``ГГГГ-ММ-ДД``.
        x_api_database: Окружение внешней системы.

    Returns:
        dict: Сводка (``scope``, ``contracts``, ``events``, ``customers``, ``attention``, ``resume``, ``pipeline``).

    Raises:
        HTTPException: 400 — неверная дата; 404 — прогона за дату не было.
    """
    from configs.ai_repo import AIRepo
    from risk_monitoring import daily

    try:
        key = daily.run_key(export_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Дата — ГГГГ-ММ-ДД")
    async with AIRepo() as repo:
        stored = (await repo.get_rm_results(x_api_database.lower(), daily.KIND, [key])).get(key)
    if not stored:
        raise HTTPException(status_code=404, detail="Прогона за эту дату не было")
    return stored["ai_analysis"]

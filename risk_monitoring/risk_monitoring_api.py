"""Клиент API анализа ИИ внешней системы риск-мониторинга (чтение и запись ``ai_analysis``)."""
import asyncio
from typing import Any, Dict

import aiohttp

from configs.config import Config
from configs.http_client_manager import HTTPClientManager
from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import RetryConfig, async_retry

logger = get_logger(__name__)

ALLOWED_TABLES = {
    "risk_monitoring_contracts",
    "risk_monitoring_eis_xml_sources",
    "risk_monitoring_files",
    # ТЗ 09.10.2026, п. 3: профиль организации (секции customer / supplier) и сводка ежедневного прогона
    "risk_monitoring_organisations",
    "risk_monitoring_daily_runs",
}


LIMITER_WAIT = 120   # наибольшее ожидание своей очереди к API, с


class RiskAPIError(Exception):
    """Ошибка, повторять которую бессмысленно (401, 404, 422 ...)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RiskAPIRetryable(RiskAPIError):
    """Временная ошибка (429, 503, 5xx)."""


RISK_API_RETRY_CONFIG = RetryConfig(
    max_retries=4,
    delay=2.0,
    backoff=2.0,
    exceptions=(RiskAPIRetryable, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError),
    log_errors=True,
)


class RiskMonitoringAPI:
    """
    Клиент API анализа ИИ Risk Monitoring.

    GET   /api/risk-monitoring/ai-analysis?table=...&id=...
    PATCH /api/risk-monitoring/ai-analysis  {table, id, ai_analysis}
    POST  /api/risk-monitoring/daily-runs   {export_date, status, stages, started_at, finished_at, ai_analysis} → id
    """

    def __init__(self, http_manager: HTTPClientManager, environment: str = None, rate: float = 0.9):
        """Настраивает адрес и токен по окружению; лимит API — 60 запросов в минуту с IP, поэтому rate ≤ 1/с безопаснее.

        Args:
            http_manager: Общий ``HTTPClientManager``.
            environment: Окружение (``dev`` / ``stage`` / ``prod``).
            rate: Запросов в секунду.
        """
        cfg = Config.get_risk_api_config(environment)
        self.url = cfg["url"]
        self.headers = cfg["headers"]
        self.http_manager = http_manager
        self.rate_limiter = TokenBucket(rate=rate)

    @staticmethod
    def _check_table(table: str) -> None:
        """Проверяет, что таблица входит в закрытый список API."""
        if table not in ALLOWED_TABLES:
            raise ValueError(f"Недопустимая таблица: {table}")

    @async_retry(RISK_API_RETRY_CONFIG)
    async def _request(self, method: str, url: str = None, **kwargs) -> Dict[str, Any]:
        """Запрос к API с лимитом частоты и повторами на 429/503/5xx (с учётом ``Retry-After``).

        Ожидание лимита частоты ограничено ``LIMITER_WAIT`` секундами: зависнуть на нём задача не может.
        """
        await asyncio.wait_for(self.rate_limiter.acquire(), timeout=LIMITER_WAIT)
        session = self.http_manager.get_session()

        async with session.request(
            method, url or self.url, headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30), **kwargs
        ) as resp:
            if resp.status in (429, 503) or resp.status >= 500:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    # уважаем Retry-After, но не зависаем надолго
                    await asyncio.sleep(min(int(retry_after), 30))
                raise RiskAPIRetryable(f"HTTP {resp.status}", resp.status)

            if resp.status != 200:
                body = (await resp.text())[:300]
                raise RiskAPIError(f"HTTP {resp.status}: {body}", resp.status)

            data = await resp.json(content_type=None)
            if data.get("status") != "ok":
                raise RiskAPIError(f"Неожиданный ответ: {str(data)[:300]}")
            return data

    async def get_record(self, table: str, record_id: int) -> Dict[str, Any]:
        """Полная запись таблицы по id (``GET /api/risk-monitoring/ai-analysis``)."""
        self._check_table(table)
        data = await self._request("GET", params={"table": table, "id": record_id})
        return data["data"]

    async def patch_ai_analysis(self, table: str, record_id: int, ai_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Меняет только ai_analysis. Значение — непустой JSON-объект или непустая строка."""
        self._check_table(table)
        if not ai_analysis:
            raise ValueError("ai_analysis не может быть пустым")
        data = await self._request(
            "PATCH",
            json={"table": table, "id": int(record_id), "ai_analysis": ai_analysis},
        )
        return data["data"]

    def _sibling(self, path: str) -> str:
        """Адрес метода API рядом с ``/ai-analysis`` (``/daily-runs``, ``/organisations``)."""
        base = self.url.rsplit("/ai-analysis", 1)[0] if "/ai-analysis" in self.url else self.url.rstrip("/")
        return f"{base}/{path}"

    @property
    def daily_runs_url(self) -> str:
        """Адрес ``POST /api/risk-monitoring/daily-runs``."""
        return self._sibling("daily-runs")

    async def upsert_daily_run(self, export_date: str, status: str = None, stages: Dict[str, Any] = None,
                               started_at: str = None, finished_at: str = None) -> Dict[str, Any]:
        """Создаёт строку ``risk_monitoring_daily_runs`` или обновляет её состояние (upsert по дате выгрузки).

        Передаются только заданные поля: у существующей записи обновляются только они; ``stages`` заменяется
        целиком. ``ai_analysis`` этим запросом не пишется — только :meth:`patch_ai_analysis` по полученному ``id``
        (API daily-runs, 21.10.2026).

        Args:
            export_date: Дата выгрузки ``ГГГГ-ММ-ДД``.
            status: Код статуса (строчные латинские буквы, цифры, ``_``): ``running`` / ``completed`` / ``failed``.
            stages: Полный набор состояний этапов.
            started_at: Начало прогона, ISO 8601 с часовым поясом.
            finished_at: Окончание прогона (не раньше начала).

        Returns:
            Dict[str, Any]: ``data`` ответа (``id``, ``export_date``, ``status``).
        """
        body: Dict[str, Any] = {"export_date": export_date}
        for k, v in (("status", status), ("stages", stages), ("started_at", started_at), ("finished_at", finished_at)):
            if v is not None:
                body[k] = v
        data = await self._request("POST", url=self.daily_runs_url, json=body)
        return data.get("data") or {}

    async def upsert_organisation(self, inn: str, kpp: str, name: str, year: int = None, short_name: str = None,
                                  full_name: str = None, ogrn: str = None) -> Dict[str, Any]:
        """Организация-поставщик: существующая по ``year + inn + kpp`` или новая (``is_external = 1``,
        ``monitoring = 0``) — ``POST /api/risk-monitoring/organisations`` (API, 21.10.2026).

        Только юридические лица: ИНН — 10 цифр, КПП — 9 цифр. Реквизиты существующей записи не меняются.

        Args:
            inn: ИНН.
            kpp: КПП.
            name: Наименование (обязательно и при повторном запросе).
            year: Год справочника (по умолчанию текущий на стороне API).
            short_name: Краткое наименование.
            full_name: Полное наименование.
            ogrn: ОГРН (13 цифр).

        Returns:
            Dict[str, Any]: ``data`` ответа (``id``, ``year``, ``inn``, ``kpp``, ``is_external``, ``monitoring``).

        Raises:
            ValueError: ИНН/КПП не юридического лица.
        """
        if not (str(inn).isdigit() and len(str(inn)) == 10 and str(kpp).isdigit() and len(str(kpp)) == 9):
            raise ValueError("API принимает только юридические лица: ИНН — 10 цифр, КПП — 9 цифр")
        body: Dict[str, Any] = {"inn": str(inn), "kpp": str(kpp), "name": str(name)[:1000]}
        for k, v in (("year", year), ("short_name", short_name), ("full_name", full_name), ("ogrn", ogrn)):
            if v is not None:
                body[k] = v
        data = await self._request("POST", url=self._sibling("organisations"), json=body)
        return data.get("data") or {}

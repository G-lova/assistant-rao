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
}


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
    """

    def __init__(self, http_manager: HTTPClientManager, environment: str = None, rate: float = 3.0):
        cfg = Config.get_risk_api_config(environment)
        self.url = cfg["url"]
        self.headers = cfg["headers"]
        self.http_manager = http_manager
        self.rate_limiter = TokenBucket(rate=rate)

    @staticmethod
    def _check_table(table: str) -> None:
        if table not in ALLOWED_TABLES:
            raise ValueError(f"Недопустимая таблица: {table}")

    @async_retry(RISK_API_RETRY_CONFIG)
    async def _request(self, method: str, **kwargs) -> Dict[str, Any]:
        await self.rate_limiter.acquire()
        session = self.http_manager.get_session()

        async with session.request(
            method, self.url, headers=self.headers,
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
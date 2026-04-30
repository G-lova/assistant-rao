# configs/http_client_manager.py
import aiohttp
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class HTTPClientManager:
    def __init__(
        self, 
        timeout: float = 60.0, 
        limit: int = 100, 
        limit_per_host: int = 20
    ):
        # ⚠️ НЕ создаём TCPConnector здесь!
        self._timeout = aiohttp.ClientTimeout(
            total=timeout, 
            connect=10.0, 
            sock_read=timeout
        )
        # Сохраняем параметры для последующего создания
        self._connector_params = {
            "limit": limit,
            "limit_per_host": limit_per_host,
            "ttl_dns_cache": 300,
            "keepalive_timeout": 30.0,
            "enable_cleanup_closed": True,
            "force_close": False,
        }
        self.session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None

    async def startup(self) -> None:
        if self.session is None or self.session.closed:
            # ✅ Создаём connector внутри async-контекста
            self._connector = aiohttp.TCPConnector(**self._connector_params)
            self.session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=self._connector,
                trust_env=True,
            )
            logger.info("✅ HTTPClientManager initialized")

    async def shutdown(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
        if self._connector and not self._connector.closed:
            await self._connector.close()
            self._connector = None
            logger.info("🔒 HTTPClientManager closed")

    def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            raise RuntimeError(
                "HTTPClientManager не инициализирован. "
                "Вызовите .startup() или используйте в контексте 'async with'."
            )
        return self.session

    async def __aenter__(self):
        await self.startup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.shutdown()
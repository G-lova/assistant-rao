"""Соединения с Postgres сервиса для хранилища знаний: один пул на задачу.

``configs.working_with_db.get_async_db_connection`` создаёт и закрывает пул на каждую операцию — для
хуков ``/evaluate-documents`` это пул на каждый документ. Внутри :func:`task_pool` все вызовы
:func:`connection` берут соединения из одного пула, который создаётся лениво (при первом обращении):
если хранилище знаний выключено, к БД никто не подключается. Вне :func:`task_pool` поведение прежнее.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Optional

from configs.config import Config


class _LazyPool:
    """Пул asyncpg, создаваемый при первом запросе соединения."""

    def __init__(self, min_size: int, max_size: int):
        """Запоминает размеры пула; сам пул ещё не создан."""
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Optional[Any] = None
        self.lock = asyncio.Lock()

    async def get(self) -> Any:
        """Возвращает пул, создавая его при первом вызове (параметры ``DB_*``)."""
        if self.pool is None:
            async with self.lock:
                if self.pool is None:
                    import asyncpg  # ленивый импорт: модуль используется и там, где драйвера нет
                    self.pool = await asyncpg.create_pool(
                        host=Config.DB_HOST, port=int(Config.DB_PORT or 5432), database=Config.DB_NAME,
                        user=Config.DB_USER, password=Config.DB_PASSWORD,
                        min_size=self.min_size, max_size=self.max_size, max_inactive_connection_lifetime=300)
        return self.pool

    async def close(self) -> None:
        """Закрывает пул, если он был создан."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None


_CURRENT: ContextVar[Optional[_LazyPool]] = ContextVar("knowledge_store_pool", default=None)


@asynccontextmanager
async def task_pool(min_size: int = 1, max_size: int = 4) -> AsyncIterator[None]:
    """Область «одна задача — один пул»: внутри неё :func:`connection` переиспользует соединения.

    Вложенный вызов использует внешний пул. Дочерние ``asyncio``-задачи наследуют область (contextvars).

    Args:
        min_size: Минимум соединений.
        max_size: Максимум соединений.
    """
    if _CURRENT.get() is not None:
        yield
        return
    holder = _LazyPool(min_size, max_size)
    token = _CURRENT.set(holder)
    try:
        yield
    finally:
        _CURRENT.reset(token)
        await holder.close()


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    """Соединение с Postgres сервиса: из пула задачи, а вне :func:`task_pool` — через временный пул.

    Yields:
        asyncpg.Connection: Открытое соединение.
    """
    holder = _CURRENT.get()
    if holder is None:
        from configs.working_with_db import get_async_db_connection
        async with get_async_db_connection() as conn:
            yield conn
        return
    pool = await holder.get()
    async with pool.acquire() as conn:
        yield conn

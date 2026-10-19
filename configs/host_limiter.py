"""Ограничение запросов к одному хосту внутри процесса: число одновременных запросов и общая пауза после 429.

Нужен для скачивания файлов через ``/api/media`` портала экспертиз: у портала ограничение частоты запросов на ключ
``SYSTEM_API_KEY``, и параллельные скачивания (несколько ссылок экспертизы, несколько экспертиз в воркере) быстро
его исчерпывают. Когда один запрос получил 429, пауза ставится на весь хост — остальные корутины тоже ждут, а не
тратят попытки впустую.

Ограничение действует в пределах процесса. Семафоры создаются отдельно для каждого event loop (Celery-задача может
запускать свой ``asyncio.run``), пауза после 429 — общая для всех loop процесса и для всех ограничителей
(скачивание ``/api/media`` и SQL-запросы ``/api/database/query`` упираются в один лимит ключа на хосте).
"""
import asyncio
import email.utils
import logging
import time
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Пауза после 429 по хостам (time.monotonic() до конца паузы) — общая для всех ограничителей процесса
_SHARED_COOLDOWN: Dict[str, float] = {}


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Разбирает заголовок ``Retry-After``: число секунд или HTTP-дата.

    Args:
        value: Значение заголовка (может отсутствовать).
        now: Текущее время для HTTP-даты (для тестов); по умолчанию — сейчас, UTC.

    Returns:
        float | None: Секунды ожидания (не меньше 0) или ``None``, если заголовка нет или он не разобран.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


def host_of(url: str) -> str:
    """Возвращает хост URL в нижнем регистре (ключ ограничителя).

    Args:
        url: Адрес запроса.

    Returns:
        str: Хост (``netloc``) или сам URL, если его не удалось разобрать.
    """
    return urlparse(url).netloc.lower() or url


class HostLimiter:
    """Ограничитель запросов по хостам: семафор на хост и общая пауза («cooldown») после 429."""

    def __init__(self, concurrency: int = 1, cooldown: Optional[Dict[str, float]] = None):
        """Создаёт ограничитель.

        Args:
            concurrency: Одновременных запросов к одному хосту в одном event loop (не меньше 1).
            cooldown: Словарь пауз по хостам; по умолчанию — общий для процесса (пауза после 429 у одного
                ограничителя действует и на другие). Отдельный словарь — для тестов.
        """
        self.concurrency = max(1, int(concurrency))
        self._cooldown_until: Dict[str, float] = _SHARED_COOLDOWN if cooldown is None else cooldown
        self._semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Semaphore]]" = (
            weakref.WeakKeyDictionary()
        )

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        """Семафор хоста для текущего event loop (создаётся при первом обращении)."""
        loop = asyncio.get_running_loop()
        per_loop = self._semaphores.setdefault(loop, {})
        if host not in per_loop:
            per_loop[host] = asyncio.Semaphore(self.concurrency)
        return per_loop[host]

    def cooldown_left(self, host: str) -> float:
        """Сколько секунд осталось до конца паузы хоста.

        Args:
            host: Хост.

        Returns:
            float: Секунды (0 — паузы нет).
        """
        return max(0.0, self._cooldown_until.get(host, 0.0) - time.monotonic())

    def penalize(self, host: str, seconds: float) -> None:
        """Ставит паузу на хост (после 429). Более длинная уже действующая пауза не сокращается.

        Args:
            host: Хост.
            seconds: Длительность паузы, секунд.
        """
        until = time.monotonic() + max(0.0, float(seconds))
        if until > self._cooldown_until.get(host, 0.0):
            self._cooldown_until[host] = until
            logger.warning(f"Хост {host}: превышен лимит запросов, пауза {seconds:.1f} с для всех запросов процесса")

    async def wait_cooldown(self, host: str) -> None:
        """Ждёт окончания паузы хоста (пауза может продлеваться, пока ждём)."""
        while True:
            left = self.cooldown_left(host)
            if left <= 0:
                return
            await asyncio.sleep(left)

    @asynccontextmanager
    async def slot(self, url: str):
        """Контекст одного запроса: занимает место в семафоре хоста и дожидается конца паузы.

        Args:
            url: Адрес запроса (берётся хост).

        Yields:
            str: Хост запроса — для ``penalize`` при ответе 429.
        """
        host = host_of(url)
        async with self._semaphore(host):
            await self.wait_cooldown(host)
            yield host


_media_limiter: Optional[HostLimiter] = None


def media_limiter() -> HostLimiter:
    """Общий ограничитель процесса для ``/api/media`` портала экспертиз (``settings.MEDIA_DOWNLOAD_CONCURRENCY``).

    Returns:
        HostLimiter: Один экземпляр на процесс.
    """
    global _media_limiter
    if _media_limiter is None:
        from configs import settings
        _media_limiter = HostLimiter(settings.MEDIA_DOWNLOAD_CONCURRENCY)
    return _media_limiter


_query_limiter: Optional[HostLimiter] = None


def query_limiter() -> HostLimiter:
    """Общий ограничитель процесса для SQL-запросов ``/api/database/query`` (``settings.PORTAL_QUERY_CONCURRENCY``).

    Пауза после 429 у него общая с :func:`media_limiter`.

    Returns:
        HostLimiter: Один экземпляр на процесс.
    """
    global _query_limiter
    if _query_limiter is None:
        from configs import settings
        _query_limiter = HostLimiter(settings.PORTAL_QUERY_CONCURRENCY)
    return _query_limiter

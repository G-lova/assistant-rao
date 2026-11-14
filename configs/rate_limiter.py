"""Ограничение частоты запросов: ``TokenBucket`` и общий на процесс лимитер запросов к модели."""
import asyncio
import time
import weakref
from typing import Optional

class TokenBucket:
    """
    Token Bucket алгоритм для ограничения частоты запросов.
    Позволяет делать до `rate` запросов в секунду с кратковременными «всплесками».
    """
    def __init__(self, rate: float, capacity: Optional[float] = None):
        self.rate = rate  # токенов в секунду (макс. частота)
        # Бак вмещает хотя бы один токен: при rate < 1 (например, 0.9 у RiskMonitoringAPI) и capacity = rate бак
        # никогда не набирал 1 токен, и acquire() ждал бесконечно — запись во внешнюю БД «висела» (20.10.2026)
        self.capacity = max(capacity or rate, 1.0)  # макс. размер «бака»
        self.tokens = self.capacity  # текущее количество токенов
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: float = 1.0) -> None:
        """Ждёт, пока в баке не появится нужное количество токенов"""
        async with self._lock:
            while True:
                now = time.monotonic()
                # Пополняем бак: сколько времени прошло × скорость
                elapsed = now - self.last_update
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_update = now
                
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return  # запрос разрешён
                
                # токенов не хватает — ждём
                wait_time = (tokens - self.tokens) / self.rate
                # Освобождаем лок на время сна, чтобы другие корутины могли пополнять бак
                self._lock.release()
                try:
                    await asyncio.sleep(wait_time)
                finally:
                    await self._lock.acquire()  # возвращаем лок для следующей итерации


LLM_RATE_DEFAULT = 3.0
"""Запросов к модели в секунду на процесс, если в ``configs.settings`` нет ``LLM_RATE_PER_SEC``."""

_llm_buckets: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, TokenBucket]" = weakref.WeakKeyDictionary()


def llm_rate() -> float:
    """Лимит запросов к модели в секунду на процесс (``configs.settings.LLM_RATE_PER_SEC``; 0 — без лимита)."""
    try:
        from configs import settings
    except Exception:  # noqa: BLE001 — модуль используется и без настроек
        return LLM_RATE_DEFAULT
    return float(getattr(settings, "LLM_RATE_PER_SEC", LLM_RATE_DEFAULT) or 0)


class SharedLLMRateLimiter:
    """Общий на процесс лимит частоты запросов к модели (вместо ``TokenBucket(rate=1.5)`` у каждого объекта).

    Раньше свой бак был у каждого объекта (``OCRProcessor``, ``TypeDetector``, ``DataExtractor``,
    ``CompletenessChecker`` …), поэтому общая частота запросов процесса ничем не ограничивалась и росла с числом
    объектов, а каждый бак лишь добавлял задержку. Теперь все экземпляры делят один бак на процесс
    (``LLM_RATE_PER_SEC``); одновременность запросов ограничивает ``configs.llm_resilience.MAX_INFLIGHT``.

    Бак создаётся отдельно для каждого цикла событий: ``asyncio.Lock`` привязан к циклу, а Celery-задача
    запускает свой ``asyncio.run``. Баки закрытых циклов удаляются сами (``WeakKeyDictionary``).
    """

    async def acquire(self, tokens: float = 1.0) -> None:
        """Ждёт разрешения на запрос (при ``LLM_RATE_PER_SEC = 0`` — сразу)."""
        rate = llm_rate()
        if rate <= 0:
            return
        loop = asyncio.get_running_loop()
        bucket = _llm_buckets.get(loop)
        if bucket is None or bucket.rate != rate:
            bucket = _llm_buckets[loop] = TokenBucket(rate=rate)
        await bucket.acquire(tokens)


def llm_rate_limiter() -> SharedLLMRateLimiter:
    """Лимитер частоты запросов к модели, общий для процесса (см. :class:`SharedLLMRateLimiter`)."""
    return SharedLLMRateLimiter()

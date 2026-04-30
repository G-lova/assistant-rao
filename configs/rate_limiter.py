import asyncio
import time
from typing import Optional

class TokenBucket:
    """
    Token Bucket алгоритм для ограничения частоты запросов.
    Позволяет делать до `rate` запросов в секунду с кратковременными «всплесками».
    """
    def __init__(self, rate: float, capacity: Optional[float] = None):
        self.rate = rate  # токенов в секунду (макс. частота)
        self.capacity = capacity or rate  # макс. размер «бака»
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
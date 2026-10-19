import asyncio
import logging
from functools import wraps
from dataclasses import dataclass

import random
from typing import Type, Tuple, Callable, Any, Optional

logger = logging.getLogger(__name__)


class EISArchiveNotReadyError(Exception):
    """Архив ЕИС ещё не готов к скачиванию (статус 425)"""
    pass

class EISRateLimitError(Exception):
    """Исключение для обработки HTTP 429 Too Many Requests"""
    def __init__(self, message: str, retry_after: int = None):
        super().__init__(message)
        self.retry_after = retry_after  # секунды до повторной попытки



class RetryConfig:
    """
    Конфигурация параметров повторных попыток выполнения операции.

    Используется для настройки поведения механизмов повторных вызовов
    при возникновении исключений: количество попыток, начальная задержка,
    экспоненциальный отступ, типы обрабатываемых исключений и необходимость логирования.
    """
    def __init__(
        self,
        max_retries: int = 3,
        delay: float = 1.0,
        backoff: float = 2.0,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
        log_errors: bool = True,
        rate_limit_delay: Optional[float] = None,
        max_delay: float = 120.0,
    ):
        """
        Инициализирует конфигурацию повторных попыток.

        Args:
            max_retries (int, optional): Максимальное количество повторных попыток.
                Defaults to 3.
            delay (float, optional): Начальная задержка между попытками в секундах.
                Defaults to 1.0.
            backoff (float, optional): Коэффициент экспоненциального увеличения задержки
                (например, при backoff=2 задержка удваивается после каждой попытки).
                Defaults to 2.0.
            exceptions (Tuple[Type[Exception], ...], optional): Кортеж типов исключений,
                при которых следует выполнять повторные попытки.
                Defaults to (Exception,) — то есть любые исключения.
            log_errors (bool, optional): Флаг, указывающий, следует ли логировать
                ошибки при каждой неудачной попытке.
                Defaults to True.
            rate_limit_delay (float, optional): Первая пауза после ``EISRateLimitError`` без ``retry_after``
                (растёт с ``backoff``). ``None`` — как для остальных ошибок (``delay``).
            max_delay (float, optional): Потолок одной паузы, секунд (в том числе для ``Retry-After``).
                Defaults to 120.
        """
        self.max_retries = max_retries
        self.rate_limit_delay = rate_limit_delay
        self.max_delay = max_delay
        self.delay = delay
        self.backoff = backoff
        self.exceptions = exceptions
        self.log_errors = log_errors


def async_retry(config: RetryConfig):
    """
    Декоратор для автоматического повторного вызова асинхронной функции при возникновении исключений.

    Поддерживает настраиваемое количество попыток, экспоненциальную задержку (backoff),
    фильтрацию типов исключений и опциональное логирование ошибок и повторных попыток.
    Используется для повышения устойчивости асинхронных операций к временным сбоям.

    Args:
        config (RetryConfig): Конфигурация повторных попыток, включающая максимальное
            количество попыток, начальную задержку, коэффициент backoff, список отслеживаемых
            исключений и флаг логирования.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(config.max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except config.exceptions as e:
                    last_exception = e
                    if "504" in str(e) or "Gateway" in str(e):
                        logger.error(f"504 Gateway Timeout в {func.__name__} (попытка {attempt+1})")
                    elif attempt < config.max_retries and config.log_errors:
                        logger.warning(f"Ошибка в {func.__name__}: {e}")
                    if attempt >= config.max_retries:
                        break
                    # пауза — всегда (раньше она пропускалась при log_errors=False); после 429 — с учётом Retry-After
                    pause = retry_delay(config, attempt + 1, e)
                    if config.log_errors:
                        logger.warning(
                            f"Повторная попытка {attempt + 1}/{config.max_retries} для {func.__name__}. "
                            f"Задержка: {pause:.2f}с"
                        )
                    await asyncio.sleep(pause)

            error_msg = (
                f"Функция {func.__name__} завершилась с ошибкой после "
                f"{config.max_retries + 1} попыток. Последняя ошибка: {str(last_exception)}"
            )
            logger.error(error_msg)
            raise last_exception

        return wrapper
    return decorator


def retry_delay(config: RetryConfig, attempt: int, error: Optional[BaseException] = None) -> float:
    """Пауза перед повторной попыткой.

    * ошибка с ``retry_after`` (ответ 429 с заголовком ``Retry-After``) — ждём столько, сколько просит сервер,
      плюс до 20 % (чтобы параллельные запросы не вернулись одновременно), не дольше ``max_delay``;
    * ``EISRateLimitError`` без ``retry_after`` при заданном ``rate_limit_delay`` — ``rate_limit_delay·backoff^(n-1)``;
    * иначе — прежняя формула ``delay·backoff^(n-1)``; во всех случаях джиттер и потолок ``max_delay``.

    Args:
        config: Конфигурация повторов.
        attempt: Номер повторной попытки, начиная с 1.
        error: Исключение предыдущей попытки.

    Returns:
        float: Секунды ожидания.
    """
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        try:
            wait = float(retry_after)
        except (TypeError, ValueError):
            wait = None
        if wait is not None:
            return min(config.max_delay, max(0.0, wait) * random.uniform(1.0, 1.2) + random.uniform(0.0, 1.0))
    base = config.delay
    if isinstance(error, EISRateLimitError) and config.rate_limit_delay is not None:
        base = config.rate_limit_delay
    delay = base * (config.backoff ** (attempt - 1))
    return min(config.max_delay, delay * random.uniform(0.7, 1.3))


def sync_retry(config: RetryConfig):
    """
    Декоратор для автоматического повторного вызова синхронной функции при возникновении исключений.

    Поддерживает настраиваемое количество попыток, экспоненциальную задержку (backoff),
    фильтрацию типов исключений и опциональное логирование ошибок и повторных попыток.
    Предназначен для повышения надёжности синхронных операций, подверженных временным сбоям.

    Args:
        config (RetryConfig): Конфигурация повторных попыток, включающая максимальное
            количество попыток, начальную задержку, коэффициент backoff, список отслеживаемых
            исключений и флаг логирования.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            current_delay = config.delay
            
            for attempt in range(config.max_retries + 1):
                try:
                    if attempt > 0 and config.log_errors:
                        logger.warning(
                            f"Повторная попытка {attempt}/{config.max_retries} "
                            f"для {func.__name__}. Задержка: {current_delay}с"
                        )
                    
                    return func(*args, **kwargs)
                    
                except config.exceptions as e:
                    last_exception = e
                    
                    if attempt == config.max_retries:
                        break
                    
                    if config.log_errors:
                        logger.error(
                            f"Ошибка в {func.__name__} (попытка {attempt + 1}/{config.max_retries}): {str(e)}"
                        )
                    
                    import time
                    time.sleep(current_delay)
                    current_delay *= config.backoff
            
            # Если все попытки исчерпаны
            error_msg = (
                f"Функция {func.__name__} завершилась с ошибкой после "
                f"{config.max_retries + 1} попыток. Последняя ошибка: {str(last_exception)}"
            )
            logger.error(error_msg)
            raise last_exception
            
        return wrapper
    return decorator


# Конфигурации для разных типов операций
API_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    delay=1.0,
    backoff=2.0,
    exceptions=(Exception, ConnectionError, TimeoutError),
    log_errors=True
)

CLOUD_PARSING_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    delay=2.0,
    backoff=2.0,
    exceptions=(Exception, ConnectionError, TimeoutError),
    log_errors=True
)

DATABASE_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    delay=1.0,
    backoff=1.5,
    exceptions=(Exception, ConnectionError),
    log_errors=True
)

EXPERTS_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    delay=1.0,
    backoff=2.0,
    exceptions=(Exception,),
    log_errors=True
)

EIS_RETRY_CONFIG = RetryConfig(
        max_retries=4,           # 1 + 4 попытки = до 32 сек ожидания
        delay=2.0,               # начальная задержка 2 сек
        backoff=2.0,             # экспоненциальный рост: 2→4→8→16
        exceptions=(EISArchiveNotReadyError, ConnectionError, TimeoutError, EISRateLimitError),
        log_errors=True
    )

def _media_retry_config() -> RetryConfig:
    """Повторы скачивания через ``/api/media`` портала: 429 ждём дольше (``settings.PORTAL_RATE_LIMIT_*``)."""
    from configs import settings
    return RetryConfig(
        max_retries=settings.MEDIA_RATE_LIMIT_RETRIES,
        delay=2.0,
        backoff=2.0,
        exceptions=(ConnectionError, TimeoutError, EISRateLimitError),
        log_errors=True,
        rate_limit_delay=settings.PORTAL_RATE_LIMIT_DELAY,
        max_delay=settings.PORTAL_RATE_LIMIT_MAX_DELAY,
    )


def _portal_query_retry_config() -> RetryConfig:
    """Повторы SQL-запросов через ``/api/database/query`` портала: только после 429, паузы как у ``/api/media``."""
    from configs import settings
    return RetryConfig(
        max_retries=settings.PORTAL_QUERY_RATE_LIMIT_RETRIES,
        delay=2.0,
        backoff=2.0,
        exceptions=(EISRateLimitError,),
        log_errors=True,
        rate_limit_delay=settings.PORTAL_RATE_LIMIT_DELAY,
        max_delay=settings.PORTAL_RATE_LIMIT_MAX_DELAY,
    )


PORTAL_QUERY_RETRY_CONFIG = _portal_query_retry_config()

# 10→20→40→60→60→60 с при 429 без Retry-After: окно ограничения портала (минута) успевает закончиться
MEDIA_RETRY_CONFIG = _media_retry_config()

LLM_RETRY_CONFIG = RetryConfig(
        max_retries=4,           # 1 + 4 попытки = до 32 сек ожидания
        delay=2.0,               # начальная задержка 2 сек
        backoff=2.0,             # экспоненциальный рост: 2→4→8→16
        exceptions=(Exception, ConnectionError, TimeoutError),
        log_errors=True
    )
import asyncio
import logging
from functools import wraps
from dataclasses import dataclass

from typing import Type, Tuple, Callable, Any, Optional


logger = logging.getLogger(__name__)


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
        log_errors: bool = True
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
        """
        self.max_retries = max_retries
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
            current_delay = config.delay
            
            for attempt in range(config.max_retries + 1):
                try:
                    if attempt > 0 and config.log_errors:
                        logger.warning(
                            f"Повторная попытка {attempt}/{config.max_retries} "
                            f"для {func.__name__}. Задержка: {current_delay}с"
                        )
                    
                    return await func(*args, **kwargs)
                    
                except config.exceptions as e:
                    last_exception = e
                    
                    if attempt == config.max_retries:
                        break
                    
                    if config.log_errors:
                        logger.error(
                            f"Ошибка в {func.__name__} (попытка {attempt + 1}/{config.max_retries}): {str(e)}"
                        )
                    
                    await asyncio.sleep(current_delay)
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


@dataclass
class RetryConfig:
    max_retries: int = 2
    delay: float = 1.0
    backoff: float = 2.0
    exceptions: Tuple[Type[Exception], ...] = (Exception,)
    log_errors: bool = False

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
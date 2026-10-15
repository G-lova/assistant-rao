"""Безопасный вызов функций хранилища знаний: сбой записи не должен ломать основной пайплайн."""
import functools
import inspect
from typing import Any, Awaitable, Callable, Optional

from configs.config import Config
from configs.logger import get_logger

logger = get_logger(__name__)


def is_enabled() -> bool:
    """Проверяет, включено ли хранилище знаний.

    Returns:
        bool: ``True``, если переменная окружения ``KNOWLEDGE_STORE_ENABLED`` равна ``true``.
            Значение читается при каждом вызове, чтобы флаг можно было менять без пересборки кода.
    """
    import os
    return os.getenv("KNOWLEDGE_STORE_ENABLED", str(Config.KNOWLEDGE_STORE_ENABLED)).lower() == "true"


async def safe_call_async(func: Callable[..., Awaitable[Any]], *args, default: Optional[Any] = None, **kwargs) -> Any:
    """Выполняет корутину хранилища с перехватом любых исключений.

    Если флаг ``KNOWLEDGE_STORE_ENABLED`` выключен, функция не вызывается вовсе.
    Любая ошибка логируется (без содержимого аргументов — там могут быть тексты документов)
    и заменяется значением ``default``.

    Args:
        func: Асинхронная функция для вызова.
        *args: Позиционные аргументы функции.
        default: Значение, возвращаемое при выключенном флаге или ошибке.
        **kwargs: Именованные аргументы функции.

    Returns:
        Any: Результат функции либо ``default``.
    """
    if not is_enabled():
        return default
    try:
        return await func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — намеренно ловим всё: хранилище не должно ломать пайплайн
        logger.error(f"knowledge_store: ошибка в {getattr(func, '__name__', func)}: {type(e).__name__}: {e}")
        return default


def safe_sync(default: Optional[Any] = None) -> Callable:
    """Декоратор для синхронных функций: перехват исключений и проверка флага.

    Args:
        default: Значение, возвращаемое при выключенном флаге или ошибке.

    Returns:
        Callable: Декоратор.
    """
    def decorator(func: Callable) -> Callable:
        """Оборачивает функцию ``func``."""
        assert not inspect.iscoroutinefunction(func), "для корутин используйте safe_call_async"

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            """Вызывает ``func`` с защитой от исключений."""
            if not is_enabled():
                return default
            try:
                return func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.error(f"knowledge_store: ошибка в {func.__name__}: {type(e).__name__}: {e}")
                return default
        return wrapper
    return decorator

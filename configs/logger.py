import logging
import os

from configs.config import Config


def setup_logging():
    """
    Настраивает систему логирования приложения.

    Создаёт директорию для файлов логов (если она не существует), устанавливает формат сообщений
    и настраивает обработчики: запись в файл и вывод в консоль. Уровень логирования, путь к файлу
    и другие параметры берутся из конфигурации приложения (класс Config). После настройки
    записывает сообщение о старте логирования.
    """
    config = Config()
    
    # Создаем директорию для логов если её нет
    log_dir = os.path.dirname(config.LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Формат логов
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Настройка базового конфига логирования
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL),
        format=log_format,
        handlers=[
            logging.FileHandler(config.LOG_FILE),
            logging.StreamHandler()  # Вывод в консоль
        ]
    )
    
    # Логируем старт приложения
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized. Level: {config.LOG_LEVEL}, File: {config.LOG_FILE}")


def get_logger(name):
    """
    Возвращает настроенный экземпляр логгера с указанным именем.

    Используется для получения объекта logging.Logger, который наследует глобальную
    конфигурацию (уровень, формат, обработчики), настроенную через `setup_logging`.
    Позволяет вести структурированное логирование с разделением по модулям/компонентам.

    Args:
        name (str): Имя логгера (обычно используется `__name__` модуля).

    Returns:
        logging.Logger: Настроенный экземпляр логгера.
    """
    return logging.getLogger(name)


# ---------------- компактные записи: обрезка длинных значений и маскировка ключей ----------------
import json as _json
import re as _re

SECRET_HEADER_RE = _re.compile(r"key|token|auth|secret|password|cookie", _re.IGNORECASE)


def log_limit() -> int:
    """Предел длины значения в записи уровня INFO (``settings.LOG_VALUE_MAX_CHARS``; 0 — без обрезки)."""
    try:
        from configs import settings
    except Exception:  # noqa: BLE001 — модуль используется и без настроек
        return 2000
    return int(getattr(settings, "LOG_VALUE_MAX_CHARS", 2000) or 0)


def brief(value, limit: int = None) -> str:
    """Значение для журнала: словарь/список — JSON в одну строку, длиннее предела — обрезано с пометкой.

    Полные результаты извлечения, ответы OCR и разбора ссылок писались в журнал целиком на уровне INFO —
    сотни КБ на документ. Полный текст остаётся на уровне DEBUG (см. вызовы) и в БД.

    Args:
        value: Значение (строка, словарь, список …).
        limit: Предел символов (по умолчанию :func:`log_limit`; 0 — без обрезки).

    Returns:
        str: Строка не длиннее предела плюс пометка «…[+N симв.]».
    """
    if isinstance(value, str):
        text = value
    else:
        try:
            text = _json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text = str(value)
    limit = log_limit() if limit is None else limit
    if limit and len(text) > limit:
        return f"{text[:limit]}…[+{len(text) - limit} симв.]"
    return text


def mask_headers(headers) -> dict:
    """Заголовки для журнала: значения ключей, токенов, авторизации и cookie заменены на ``***`` (+ длина).

    Args:
        headers: Словарь заголовков (или ``None``).

    Returns:
        dict: Копия с замаскированными секретами.
    """
    out = {}
    for k, v in dict(headers or {}).items():
        out[k] = f"*** (len={len(str(v))})" if SECRET_HEADER_RE.search(str(k)) and v else v
    return out


# ---------------- контекст журнала: «[prod:7185] …» у всех записей задачи ----------------
import contextvars
from contextlib import contextmanager

_LOG_CONTEXT: "contextvars.ContextVar[str]" = contextvars.ContextVar("log_context", default="")
_FACTORY_INSTALLED = False


@contextmanager
def log_context(label: str):
    """Добавляет метку ко всем записям журнала внутри блока (и в порождённых им asyncio-задачах).

    Несколько экспертиз обрабатываются одним воркером параллельно, и строки разных прогонов перемешивались:
    по записи «Документ 'Договор.pdf' …» нельзя было понять, к какой экспертизе она относится. Метка
    (обычно ``окружение:id``) попадает в начало сообщения: ``[prod:7185] Документ 'Договор.pdf' …``. Вложенные
    метки соединяются через « / ».

    Args:
        label: Метка; пустая — без изменений.

    Yields:
        None
    """
    _install_record_factory()
    if not label:
        yield
        return
    current = _LOG_CONTEXT.get()
    token = _LOG_CONTEXT.set(f"{current} / {label}" if current else str(label))
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def current_log_context() -> str:
    """Текущая метка журнала (пустая строка вне :func:`log_context`)."""
    return _LOG_CONTEXT.get()


def _install_record_factory() -> None:
    """Подключает к ``logging`` фабрику записей, добавляющую метку контекста (один раз на процесс).

    Фабрика, а не фильтр обработчика: обработчики журнала у Celery и у приложения разные, а фабрика действует на
    все записи независимо от обработчиков.
    """
    global _FACTORY_INSTALLED
    if _FACTORY_INSTALLED:
        return
    original = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        """Создаёт запись и добавляет метку контекста в начало сообщения."""
        record = original(*args, **kwargs)
        label = _LOG_CONTEXT.get()
        if label and isinstance(record.msg, str) and not record.msg.startswith(f"[{label}]"):
            record.msg = f"[{label}] {record.msg}"
        return record

    logging.setLogRecordFactory(factory)
    _FACTORY_INSTALLED = True

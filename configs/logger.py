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

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

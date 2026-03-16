import asyncio
import json
import sys
import os
from datetime import date

from configs.config import Config
from configs.logger import get_logger
from conclusion.conclusion_pipeline import RaoConclusionPipeline


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

# Настройка логирования
logger = get_logger(__name__)

async def rao_conclusion(expertise_id: int, x_api_database: str, send_to_external: bool = False) -> dict:
    """
    Формирует сводное заключение эксперта РАО для заданной экспертизы.

    Создает экземпляр RaoConclusionPipeline, запускает анализ всех документов
    экспертизы, формирует заключение на основе результатов анализа и
    опционально отправляет результаты во внешние системы.

    Args:
        expertise_id: Уникальный идентификатор экспертизы
        x_api_database: Идентификатор среды базы данных ('dev', 'prod', 'stage')
        send_to_external: Флаг отправки результатов во внешние системы

    Returns:
        dict: Сформированное заключение эксперта РАО с результатами анализа

    Raises:
        Exception: При ошибках в пайплайне формирования заключения
    """
    try:
        logger.info("Starting rao_conclusion pipeline...")

        # Создание пайплайна
        pipeline = RaoConclusionPipeline(expertise_id, x_api_database)
        logger.info(f"Processing expertise_id: {expertise_id}, DB: {x_api_database}")

        # Запуск пайплайна
        rao_conclusion = await pipeline.run_pipeline()
        
        # Отправка результатов
        await pipeline.send_rao_conclusion(rao_conclusion, send_to_external)
        logger.info("Rao_conclusion pipeline completed successfully")
        
        # Вывод результатов
        logger.info(f'Итоговое заключение:\n{rao_conclusion}')
        
        return rao_conclusion
        
    except Exception as e:
        logger.error(f"Error in rao_conclusion pipeline: {e}", exc_info=True)
        raise

if __name__ == "__main__":

    expertise_id = int(input('ID экспертизы для создания сводного заключения эксперта РАО:'))
    asyncio.run(rao_conclusion(expertise_id))
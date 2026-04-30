import asyncio
import json
import sys
import os
from datetime import date

from configs.config import Config
from configs.http_client_manager import HTTPClientManager
from configs.logger import get_logger
from conclusion.conclusion_pipeline import RaoConclusionPipeline


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

# Настройка логирования
logger = get_logger(__name__)

async def rao_conclusion(expertise_id, x_api_database, http_manager: HTTPClientManager, send_to_external = False):
    """
    Запускает пайплайн оценки экспертов для заданной экспертизы.

    Функция инициализирует пайплайн ScoringPipeline, выполняет SQL-запрос для получения данных
    об экспертах, рассчитывает рейтинг на основе критериев и возвращает отсортированный список
    экспертов по убыванию релевантности. Результаты сохраняются в CSV-файл.

    Args:
        expertise_id: Идентификатор экспертизы, для которой проводится оценка и подбор экспертов.

    Returns:
        pd.Series или pd.DataFrame: Отсортированный набор результатов (топ экспертов),
                                   готовый к использованию или выводу.
                                   В случае ошибки — исключение не подавляется.
    """
    
    try:
        logger.info("Starting rao_conclusion pipeline...")
        
        # Создание пайплайна
        pipeline = RaoConclusionPipeline(http_manager, expertise_id, x_api_database)        
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
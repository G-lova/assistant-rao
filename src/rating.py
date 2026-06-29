import asyncio
from datetime import date
import json
import sys
import os

from configs.config import Config
from configs.http_client_manager import HTTPClientManager
from search_experts.pipeline import RatingPipeline
from configs.logger import get_logger


current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

# Настройка логирования
logger = get_logger(__name__)

<<<<<<< HEAD
async def rating(start_date, end_date, x_api_database):
=======
async def rating(start_date, end_date, x_api_database, http_manager: HTTPClientManager):
>>>>>>> develop
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
        logger.info("Starting rating pipeline...")
        
        # Создание пайплайна
        pipeline = RatingPipeline(http_manager, x_api_database)
        
        # Параметры запуска
        sql_file_name = "experts_rating.sql"
        
        logger.info(f"Processing {start_date}-{end_date} rating pipeline...")
        
        # Запуск пайплайна и получение результатов
        ratings = await pipeline.get_experts_rating(sql_file_name, start_date, end_date)
        
        logger.info("Rating pipeline completed successfully")
        
        # Вывод результатов
        print(ratings)
        
<<<<<<< HEAD
=======
        # # Сохранение результатов
        # output_dir = os.path.join(project_root, "data", "outputs")
        # os.makedirs(output_dir, exist_ok=True)

        # output_file = os.path.join(output_dir, f"{start_date}_{end_date}_rating_results_{date.today()}.json")
        # with open(output_file, "w", encoding="utf-8") as f:
        #     json.dump(ratings, f, ensure_ascii=False, indent=2)
        
        # logger.info(f"Results saved to: {output_file}")
        
>>>>>>> develop
        return ratings
        
    except Exception as e:
        logger.error(f"Error in rating pipeline: {e}", exc_info=True)
        raise

if __name__ == "__main__":

    start_date = input('Дата начала интервала:')
    end_date = input('Дата окончания интервала:')
<<<<<<< HEAD
    asyncio.run(rating(start_date, end_date, "prod"))
=======
    asyncio.run(rating(start_date, end_date, "dev", http_manager))
>>>>>>> develop

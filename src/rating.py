from datetime import date
import json
import sys
import os

from configs.config import Config
from search_experts.pipeline import RatingPipeline
from configs.logger import get_logger


current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

# Настройка логирования
logger = get_logger(__name__)

def rating(start_date: str, end_date: str, x_api_database: str) -> dict:
    """
    Рассчитывает рейтинги экспертов за указанный период времени.

    Создает экземпляр RatingPipeline, выполняет SQL-запрос из файла
    experts_rating.sql для получения данных о работе экспертов в заданном
    периоде и рассчитывает их рейтинги.

    Args:
        start_date: Начальная дата периода в формате YYYY-MM-DD
        end_date: Конечная дата периода в формате YYYY-MM-DD
        x_api_database: Идентификатор среды базы данных ('dev', 'prod', 'stage')

    Returns:
        dict: Словарь с рейтингами экспертов в формате {expert_id: rating}

    Raises:
        Exception: При ошибках в пайплайне расчета рейтингов
    """
    try:
        logger.info("Starting rating pipeline...")

        # Создание пайплайна
        pipeline = RatingPipeline(x_api_database)

        # Параметры запуска
        sql_file_name = "experts_rating.sql"

        logger.info(f"Processing {start_date}-{end_date} rating pipeline...")

        # Запуск пайплайна и получение результатов
        ratings = pipeline.get_experts_rating(sql_file_name, start_date, end_date)

        logger.info("Rating pipeline completed successfully")

        # Вывод результатов
        print(ratings)

        # # Сохранение результатов
        # output_dir = os.path.join(project_root, "data", "outputs")
        # os.makedirs(output_dir, exist_ok=True)

        # output_file = os.path.join(output_dir, f"{start_date}_{end_date}_rating_results_{date.today()}.json")
        # with open(output_file, "w", encoding="utf-8") as f:
        #     json.dump(ratings, f, ensure_ascii=False, indent=2)

        # logger.info(f"Results saved to: {output_file}")

        return ratings
        
    except Exception as e:
        logger.error(f"Error in rating pipeline: {e}", exc_info=True)
        raise

if __name__ == "__main__":

    start_date = input('Дата начала интервала:')
    end_date = input('Дата окончания интервала:')
    rating()
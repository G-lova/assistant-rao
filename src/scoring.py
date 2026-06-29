import asyncio
import sys
import os
from datetime import date

from configs.config import Config
from configs.http_client_manager import HTTPClientManager
from search_experts.pipeline import ScoringPipeline
from configs.logger import get_logger


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

logger = get_logger(__name__)

<<<<<<< HEAD
async def scoring(expertise_id: int, details: bool, x_api_database: str) -> list:
=======
async def scoring(expertise_id: int, details: bool, x_api_database: str, http_manager: HTTPClientManager) -> list:
>>>>>>> develop
    """
    Запускает пайплайн скоринга экспертов для заданной экспертизы.

    Создает экземпляр ScoringPipeline, выполняет SQL-запрос из файла
    experts_for_expertise.sql для получения данных об экспертах,
    рассчитывает рейтинг на основе ML-модели и возвращает отсортированный
    список наиболее подходящих экспертов.

    Args:
        expertise_id: Уникальный идентификатор экспертизы
        details: Флаг, указывающий на необходимость получения подробной информации о каждом эксперте (по умолчанию False)
        x_api_database: Идентификатор среды базы данных ('dev', 'prod', 'stage')

    Returns:
        list: Отсортированный список ID экспертов по убыванию релевантности

    Raises:
        Exception: При ошибках в пайплайне скоринга экспертов
    """
    # Настройка логирования

    try:
        logger.info("Starting scoring pipeline...")

        # Создание пайплайна
<<<<<<< HEAD
        pipeline = ScoringPipeline(x_api_database)
=======
        pipeline = ScoringPipeline(http_manager, x_api_database)
>>>>>>> develop

        # Параметры запуска
        sql_file_name = "experts_for_expertise.sql"

        logger.info(f"Processing expertise_id: {expertise_id}, DB: {x_api_database}")

        # Запуск пайплайна
        df = await pipeline.run_pipeline(sql_file_name, expertise_id)

        # Получение результатов
        top_results = await asyncio.to_thread(pipeline.get_top_results, df, details)

        logger.info(f"Scoring pipeline completed successfully: expertise_id={expertise_id}, DB={x_api_database}, details={details}")

        # Вывод результатов
        print(f'Эксперты в порядке убывания рейтинга:\n{top_results}')

        # # Сохранение результатов
        # output_dir = os.path.join(project_root, "data", "outputs")
        # os.makedirs(output_dir, exist_ok=True)

        # output_file = os.path.join(output_dir, f"expertise_{expertise_id}_results_{date.today()}.csv")
        # top_results.to_csv(output_file, index=False, encoding='utf-8')

        # logger.info(f"Results saved to: {output_file}")

        return top_results

    except Exception as e:
        logger.error(f"Error in scoring pipeline: {e}", exc_info=True)
        raise

<<<<<<< HEAD
    finally:
        # закрытие клиента
        if pipeline and hasattr(pipeline, "embedding_client"):
            await pipeline.embedding_client.close()
=======
    # finally:
    #     # закрытие клиента
    #     if pipeline and hasattr(pipeline, "embedding_client"):
    #         await pipeline.embedding_client.close()
>>>>>>> develop

if __name__ == "__main__":

    expertise_id = int(input('ID экспертизы для подбора эксперта:'))
<<<<<<< HEAD
    asyncio.run(scoring(expertise_id, False, "dev"))
=======
    asyncio.run(scoring(expertise_id, False, "dev", http_manager))
>>>>>>> develop

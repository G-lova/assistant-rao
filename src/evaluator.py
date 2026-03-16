import asyncio
import sys
import os

from evaluate_documents.evaluate_documents_pipeline import EvaluateDocumentsPipeline
from configs.logger import get_logger
from tasks import evaluate_documents_task


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

# Настройка логирования
logger = get_logger(__name__)

async def evaluator(expertise_id: int, x_api_database: str) -> dict:
    """
    Запускает асинхронный пайплайн оценки документов для экспертизы.

    Создает экземпляр EvaluateDocumentsPipeline, выполняет анализ всех документов
    экспертизы согласно SQL-скрипту evaluate_docs_script.sql, и запускает
    Celery-задачу для фоновой обработки результатов.

    Args:
        expertise_id: Уникальный идентификатор экспертизы для анализа
        x_api_database: Идентификатор среды базы данных ('dev', 'prod', 'stage')

    Returns:
        dict: Информация о запущенной задаче:
            - task_id (str): ID Celery-задачи для отслеживания
            - status (str): Статус выполнения ("processing")
            - message (str): Описание состояния

    Raises:
        Exception: При ошибках в пайплайне обработки документов
    """
    try:
        logger.info("Starting evaluator pipeline...")

        # Создание пайплайна
        pipeline = EvaluateDocumentsPipeline(x_api_database)

        # Параметры запуска
        sql_file_name = "evaluate_docs_script.sql"

        logger.info(f"Processing expertise_id: {expertise_id}, DB: {x_api_database}")

        # Запуск пайплайна и получение датафрейма
        df = await pipeline.run_pipeline(sql_file_name, expertise_id)

        # Передача результатов в task и запуск задачи
        task = evaluate_documents_task.delay(df=df.to_dict(orient="records"))

        logger.info(f"""Принят запрос на анализ документов для экспертизы: {expertise_id} (task_id: {task.id})""")

        return {
            "task_id": task.id,
            "status": "processing",
            "message": "Задача запущена"
        }

    except Exception as e:
        logger.error(f"Error in evaluator pipeline: {e}", exc_info=True)
        raise

if __name__ == "__main__":

    expertise_id = int(input('ID экспертизы для проверки документов:'))
    asyncio.run(evaluator(expertise_id))
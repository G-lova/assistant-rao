import asyncio
from configs.http_client_manager import HTTPClientManager

from celery import current_task
from celery_app import celery_app

from configs.logger import get_logger
from evaluate_documents.evaluate_documents_pipeline import TasksPipeline
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


logger = get_logger(__name__)
        

def run_async(coro):
    """Запускает корутину с правильным управлением event loop"""
    return asyncio.run(coro)


@celery_app.task(bind=True, name="evaluate_documents_task")
def evaluate_documents_task(self, expertise_id: int, environment: str):
    """
    Запускает полный конвейер оценки экспертов для заданной экспертизы.

    Последовательно выполняет все этапы: загрузку данных, предобработку, расчёт сходства,
    фильтрацию по конфликтам и вычисление рейтинга.

    Args:
        sql_file_path (str): Имя файла с SQL-запросом для получения данных об экспертах.
        expertise_id (str или int): Уникальный идентификатор экспертизы.

    Returns:
        pandas.DataFrame: Датафрейм с отфильтрованными и ранжированными экспертами,
        содержащий колонки 'expert_id' и 'rating'.
    """
    # ✅ Инициализируем менеджер внутри асинхронного контекста задачи

    # @async_retry(CLOUD_PARSING_RETRY_CONFIG)
    async def run_with_manager():
        async with HTTPClientManager(timeout=90.0) as mgr:
            tasks_piplene = TasksPipeline(mgr, expertise_id, environment)
            task_result = await tasks_piplene.run_pipeline()
            return task_result
        
    return run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(run_with_manager)())
    # return asyncio.run(run_with_manager)
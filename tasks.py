import asyncio
from configs.http_client_manager import HTTPClientManager

from celery import current_task
from celery_app import celery_app

from configs.logger import get_logger
from evaluate_documents.evaluate_documents_pipeline import TasksPipeline
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


logger = get_logger(__name__)


def run_async(coro):
    """
    Вспомогательная функция для запуска асинхронной корутины в синхронном контексте Celery.

    Args:
        coro: Асинхронная корутина, которую нужно выполнить.

    Returns:
        Результат выполнения корутины.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, name="evaluate_documents_task")
def evaluate_documents_task(self, expertise_id: int, environment: str):
    """
    Асинхронная задача для оценки документов. Инициализирует менеджер HTTP-клиента и запускает конвейер задач.

    Args:
        expertise_id (int): Идентификатор экспертизы.
        environment (str): Окружение (например, "production", "staging").

    Returns:
        Результат выполнения конвейера задач.
    """
    # ✅ Инициализируем менеджер внутри асинхронного контекста задачи
    async def run_with_manager():
        async with HTTPClientManager(timeout=90.0) as mgr:
            tasks_piplene = TasksPipeline(mgr, expertise_id, environment)
            task_result = await tasks_piplene.run_pipeline()
            return task_result
        
    return run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(run_with_manager)())
import json
import os
import pandas as pd
import tempfile
import asyncio
from openai import OpenAI, AsyncOpenAI
import datetime

from celery import current_task
from celery_app import celery_app

from configs.config import Config
from configs.file_reader import read_file
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.utils import split_large_text
from configs.working_with_db import save_raw_data, save_summary_report, delete_procurement_data
from evaluate_documents.completeness_checker import CompletenessChecker
from evaluate_documents.consistency_checker import ConsistencyChecker
from evaluate_documents.evaluate_documents_pipeline import TasksPipeline
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG
from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING, TypeDataExtractor


logger = get_logger(__name__)

def run_async(coro):
    """
    Выполняет асинхронную корутину в синхронном контексте Celery.

    Создает новый event loop для выполнения асинхронной корутины
    в синхронной Celery-задаче.

    Args:
        coro: Асинхронная корутина для выполнения

    Returns:
        Результат выполнения корутины
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(bind=True, name="evaluate_documents_task")
def evaluate_documents_task(self, df):
    """
    Celery-задача для оценки документов экспертизы.

    Запускает полный пайплайн анализа документов: извлечение типов данных,
    проверку полноты и一致ности документов экспертизы.

    Args:
        df: DataFrame или список словарей с данными документов экспертизы

    Returns:
        dict: Результаты анализа документов
    """
    tasks_piplene = TasksPipeline(df)
    task_result = run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(tasks_piplene.run_pipeline)())
    return task_result

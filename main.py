import os
import tempfile
import logging
import json

from configs.config import Config
from configs.parsing import CloudStorageParser
from configs.schemas import EISParseRequest, RAOConclusionRequest
from src.evaluator import evaluator
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict
from celery.result import AsyncResult

from configs.parsing import CloudStorageParser
from configs.utils import APIKeyMiddleware
from configs.working_with_db import get_contract_info_from_db
from tasks import evaluate_documents_task
from configs.logger import setup_logging, get_logger
from src.rao_conclusion import rao_conclusion
from src.rating import rating
from src.scoring import scoring

app = FastAPI(debug=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(APIKeyMiddleware)

setup_logging()
logger = get_logger(__name__)

@app.post("/rao_conclusion")
async def get_rao_conclusion(
    request: RAOConclusionRequest,
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Создает сводный отчет эксперта РАО для заданной экспертизы.

    Принимает идентификатор экспертизы и флаг отправки во внешние системы,
    запускает пайплайн формирования заключения РАО и возвращает результаты анализа.

    Args:
        request: Запрос с параметрами заключения РАО, содержащий:
            - expertise_id (int): Идентификатор экспертизы
            - send_to_external (bool): Флаг отправки результатов во внешние системы
        x_api_database: Заголовок с указанием среды базы данных ('dev', 'prod', 'stage')

    Returns:
        dict: Результаты анализа документов экспертизы в формате заключения РАО

    Raises:
        HTTPException: 400 - если отсутствует expertise_id
        HTTPException: 500 - при ошибке обработки
    """
    try:
        expertise_id = request.expertise_id
        send_to_external = request.send_to_external

        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")

        # Запускаем пайплайн
        results = await rao_conclusion(expertise_id, x_api_database, send_to_external)

        return results

    except Exception as e:
        logger.error(f"Ошибка при создании сводного отчета эксперта РАО: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при создании сводного отчета эксперта РАО: {str(e)}")

# Глобальный словарь для хранения результатов анализа документов
document_analysis_cache = {}

@app.post("/parse-eis")
async def parse_eis(request: EISParseRequest):
    """
    Парсит ссылку на закупку ЕИС и извлекает связанные документы.

    Принимает ссылку на закупку в ЕИС, подключается к SOAP API ЕИС
    и извлекает все доступные документы для данной закупки.

    Args:
        request: Запрос с ссылкой на ЕИС, содержащий:
            - eis_link (str): Ссылка на закупку в ЕИС

    Returns:
        dict: Информация о найденных документах

    Raises:
        HTTPException: 400 - если ссылка на ЕИС не указана или пустая
    """
    eis_link = request.eis_link.strip()
    if not eis_link:
        raise HTTPException(status_code=400, detail="Поле 'eis_link' обязательно")

    cloud_parser = CloudStorageParser()
    files = await cloud_parser._parse_eis_soap_ip(eis_link, 123)
    return files


@app.post("/evaluate-documents")
async def evaluate_documents_batch(
    request_body: Dict[str, int] = Body(...),
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Запускает асинхронную оценку документов для заданной экспертизы.

    Принимает идентификатор экспертизы, запускает Celery-задачу для анализа
    всех документов экспертизы и возвращает ID задачи для отслеживания прогресса.

    Args:
        request_body: Тело запроса с обязательным полем:
            - expertise_id (int): Идентификатор экспертизы для анализа
        x_api_database: Заголовок с указанием среды базы данных ('dev', 'prod', 'stage')

    Returns:
        dict: Информация о запущенной задаче с полем task_id

    Raises:
        HTTPException: 400 - если отсутствует expertise_id
        HTTPException: 500 - при ошибке запуска анализа
    """
    try:
        expertise_id = request_body.get("expertise_id")

        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")

        # Запускаем задачу
        task = await evaluator(expertise_id, x_api_database)

        return task

    except Exception as e:
        logger.error(f"Ошибка при анализе документов: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при анализе документов: {str(e)}")


@app.get("/task/{task_id}")
async def get_task_status(task_id: str):
    """
    Получает статус выполнения асинхронной задачи по ее ID.

    Проверяет состояние Celery-задачи и возвращает текущий статус выполнения,
    результат или информацию об ошибке.

    Args:
        task_id: Уникальный идентификатор задачи Celery

    Returns:
        dict: Статус задачи:
            - status: "pending" - задача в очереди
            - status: "completed", result: <данные> - задача выполнена успешно
            - status: "failed", error: <описание> - задача завершилась с ошибкой
            - status: <другое> - промежуточное состояние задачи
    """
    task = AsyncResult(task_id)
    if task.state == 'PENDING':
        logger.info(f"Обработка документов в процессе: status: pending")
        return {"status": "pending"}
    elif task.state == 'FAILURE':
        logger.info(f"Ошибка обработки документов: status: failed, error: {str(task.info)}")
        return {"status": "failed", "error": str(task.info)}
    elif task.state == 'SUCCESS':
        logger.info(f"Обработка документов завершена: status: completed, result: {task.result}")
        return {"status": "completed", "result": task.result}
    else:
        logger.info(f"Обработка документов: status: {task.state}")
        return {"status": task.state}


@app.post("/get-contract-info")
async def api_get_contract_info(request_body: Dict[str, str] = Body(...)):
    """
    Получает ключевые реквизиты контракта по идентификатору закупки.

    Извлекает из базы данных номер, сумму и дату контракта на основе данных,
    ранее сохраненных в поле contract_draft таблицы raw_document_data.
    Возвращает информацию в виде JSON для быстрого доступа к основным параметрам контракта.

    Args:
        request_body: Тело запроса в формате JSON с обязательным полем:
            - procurement_id (str): Идентификатор закупки

    Returns:
        dict: Реквизиты контракта:
            - contract_number (str): Номер контракта или "0" если не найден
            - amount (str): Сумма контракта или "0" если не найдена
            - date (str): Дата контракта или "0" если не найдена

    Raises:
        HTTPException: 400 - если отсутствует procurement_id
    """
    procurement_id = request_body.get("procurement_id")
    logger.info(f"Получение данных контракта для экспертизы {procurement_id}")

    if not procurement_id:
        raise HTTPException(status_code=400, detail="Поле 'procurement_id' обязательно в теле запроса")

    try:
        info = await get_contract_info_from_db(procurement_id)
        logger.info(f"Обработка документов завершена: {info}")
        return info
    except Exception as e:
        logger.error(f"Неожиданная ошибка при обработке /get-contract-info: {e}", exc_info=True)
        return {"contract_number": "0", "amount": "0", "date": "0"}


@app.post("/get-experts-for-expertise")
async def get_experts_for_expertise(
    request_body: Dict[str, int] = Body(...),
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Подбирает список идентификаторов экспертов для заданной экспертизы.

    Принимает идентификатор экспертизы, запускает ML-пайплайн скоринга
    и возвращает отсортированный список ID экспертов, наиболее подходящих
    для проведения экспертизы. При временных сбоях автоматически повторяет
    запрос согласно настройкам EXPERTS_RETRY_CONFIG.

    Args:
        request_body: Тело запроса с обязательным полем:
            - expertise_id (int): Идентификатор экспертизы
        x_api_database: Заголовок с указанием среды базы данных ('dev', 'prod', 'stage')

    Returns:
        List[int]: Список идентификаторов экспертов, отобранных моделью скоринга

    Raises:
        HTTPException: 400 - если отсутствует expertise_id
        HTTPException: 500 - при ошибке выполнения скоринга
    """
    try:
        expertise_id = request_body.get("expertise_id")

        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")

        # Запускаем скоринг пайплайн
        results = await scoring(expertise_id, x_api_database)

        # Преобразуем результат в список целых чисел
        if hasattr(results, 'tolist'):
            expert_ids = results.tolist()
        elif isinstance(results, list):
            expert_ids = results
        else:
            col = 'expert_id' if 'expert_id' in results.columns else results.columns[0]
            expert_ids = results[col].tolist()

        expert_ids = [int(x) for x in expert_ids]

        return expert_ids
        
    except Exception as e:
        logger.error(f"Ошибка при подборе экспертов: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при подборе экспертов: {str(e)}")


@app.post("/get-experts-rating")
async def get_experts_rating(
    request_body: Dict[str, str] = Body(...),
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Возвращает рейтинги экспертов за указанный период времени.

    Выполняет SQL-запрос из файла experts_rating.sql через внешний API
    (в зависимости от окружения в X-API-Database) и возвращает словарь
    с рейтингами экспертов в формате {expert_id: rating}.

    Args:
        request_body: Тело запроса с обязательными полями:
            - start_date (str): Начальная дата периода (формат YYYY-MM-DD)
            - end_date (str): Конечная дата периода (формат YYYY-MM-DD)
        x_api_database: Заголовок с указанием среды базы данных ('dev', 'prod', 'stage')

    Returns:
        dict: Словарь рейтингов экспертов в формате {expert_id: rating}

    Raises:
        HTTPException: 400 - если не указаны start_date или end_date
        HTTPException: 500 - при ошибке получения рейтингов
    """
    try:
        start_date = request_body.get("start_date")
        end_date = request_body.get("end_date")

        if not start_date or not end_date:
            raise HTTPException(
                status_code=400,
                detail="Указание временного периода обязательно"
            )

        # Вызываем бизнес-логику (например, метод rating)
        ratings = rating(start_date, end_date, x_api_database)

        logger.info(
            f"Успешно Получено {len(ratings)} рейтингов экспертов (DB: {x_api_database}, период: {start_date}–{end_date})"
        )

        return ratings

    except Exception as e:
        logger.error(f"Ошибка при получении рейтингов экспертов: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при получении рейтингов экспертов: {str(e)}")


@app.get("/health")
async def health_check():
    """
    Эндпоинт проверки работоспособности сервиса (health check).

    Возвращает простой JSON-ответ, подтверждающий, что сервис запущен и принимает запросы.
    Используется системами мониторинга и оркестрации (например, Kubernetes) для определения состояния приложения.

    Returns:
        dict: Словарь с информацией о статусе и названии сервиса:
            - status (str): Текущее состояние ("healthy").
            - service (str): Название микросервиса ("procurement-document-analyzer").
    """
    return {"status": "healthy", "service": "procurement-document-analyzer"}
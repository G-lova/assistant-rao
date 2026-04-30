import base64
import datetime
import io
import os
import re
import tempfile
import logging
import json
import zipfile

from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Header, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from typing import List, Optional, Dict
from celery.result import AsyncResult

from configs.config import Config
from configs.schemas import EISParseRequest, EvaluateRequest, ExpertsScoringRequest, RAOConclusionRequest
from configs.eis_parsing import EISParser
from configs.utils import APIKeyMiddleware
from configs.working_with_db import get_contract_info_from_db
from configs.procurement_requirements import DOCUMENT_CODE_TO_LABEL
from configs.logger import setup_logging, get_logger
from src.law_detector import LawDetector
from src.rao_conclusion import rao_conclusion
from src.rating import rating
from src.scoring import scoring
from tasks import evaluate_documents_task

# main.py
from configs.http_client_manager import HTTPClientManager

# Глобальный экземпляр (настраивается под вашу нагрузку)
http_manager = HTTPClientManager(timeout=120.0, limit=50)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await http_manager.startup()
    app.state.http_manager = http_manager  # Делаем доступным через app.state
    yield
    await http_manager.shutdown()

app = FastAPI(lifespan=lifespan)
# app = FastAPI(debug=False)

# Функция зависимости для FastAPI-эндпоинтов
def get_http_manager() -> HTTPClientManager:
    return app.state.http_manager


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(APIKeyMiddleware)

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)
setup_logging()
logger = get_logger(__name__)

# Глобальный словарь для хранения результатов анализа документов
document_analysis_cache = {}


@app.post("/evaluate-documents")
async def evaluate_documents_batch(
    request: EvaluateRequest,
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    try:
        expertise_id = request.expertise_id        
        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")
        
        logger.info(f"Принят запрос на анализ документов для экспертизы: {expertise_id}, DB: {x_api_database}")
        task = evaluate_documents_task.delay(expertise_id, x_api_database)

        return {
            "task_id": task.id,
            "status": "processing",
            "message": "Задача запущена"
        }
        
    except Exception as e:
        logger.error(f"Ошибка при анализе документов: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при анализе документов: {str(e)}")


@app.get("/task/{task_id}")
async def get_task_status(task_id: str):
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
    Получает информацию о контракте для заданного идентификатора закупки.
    Принимает в теле запроса идентификатор закупки, извлекает из базы данных информацию о контракте и возвращает ее.
    В случае ошибок возвращает соответствующий HTTP статус и сообщение.

    Args:
        request_body (Dict[str, str]): Тело запроса в формате JSON с полем:
            - procurement_id (str): Идентификатор закупки, для которой необходимо получить информацию о контракте

    Raises:        
        HTTPException: 400 - если отсутствует procurement_id
        HTTPException: 500 - при ошибке получения информации о контракте

    Returns:
        Dict[str, str]: Словарь с информацией о контракте, содержащий поля:
            - contract_number (str): Номер контракта
            - amount (str): Сумма контракта
            - date (str): Дата заключения контракта
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
    request: ExpertsScoringRequest,
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
        expertise_id = request.expertise_id
        details = request.details

        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")

        # Запускаем скоринг пайплайн
        results = await scoring(expertise_id, details, x_api_database, http_manager)

        return results
        
    except Exception as e:
        logger.error(f"Ошибка при подборе экспертов: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при подборе экспертов: {str(e)}")


@app.post("/get-experts-rating")
async def get_experts_rating(
    request_body: Dict[str, str] = Body(...),
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Получает рейтинг экспертов для заданного временного периода.
    Принимает в теле запроса даты начала и конца периода, за который необходимо получить рейтинг. 
    Вызывает бизнес-логику для получения рейтинга экспертов и возвращает результат. 
    В случае ошибок возвращает соответствующий HTTP статус и сообщение.

    Args:
        request_body (Dict[str, str]): Тело запроса в формате JSON с полями:
            - start_date (str): Дата начала периода в формате "YYYY-MM-DD"
            - end_date (str): Дата конца периода в формате "YYYY-MM-DD"
        x_api_database (str): Заголовок с указанием среды базы данных ('dev', 'prod', 'stage')

    Raises:
        HTTPException: 400 - если отсутствуют start_date или end_date
        HTTPException: 500 - при ошибке получения рейтинга экспертов

    Returns:
        List[Dict]: Список словарей с рейтингами экспертов, полученными из бизнес-логики. Каждый словарь может содержать информацию об эксперте и его рейтинге.
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
        ratings = await rating(start_date, end_date, x_api_database, http_manager)

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

@app.post("/rao_conclusion")
async def get_rao_conclusion(
    request: RAOConclusionRequest,
    x_api_database: str = Header(default="dev", alias="X-API-Database")
    ):
    '''
    Получает сводный отчет эксперта РАО для заданной экспертизы.
    Принимает идентификатор экспертизы, запускает ML-пайплайн для генерации сводного отчета эксперта РАО и возвращает результат.
    В случае ошибок возвращает соответствующий HTTP статус и сообщение.

    Args:
        request (RAOConclusionRequest): Тело запроса с обязательным полем:
            - expertise_id (int): Идентификатор экспертизы
            - send_to_external (bool, optional): Флаг, указывающий, отправлять ли результат во внешнюю систему (по умолчанию False)
        x_api_database (str): Заголовок с указанием среды базы данных ('dev', 'prod', 'stage')

    Raises:
        HTTPException: 400 - если отсутствует expertise_id
        HTTPException: 500 - при ошибке генерации сводного отчета эксперта РАО
        
    Returns:
        dict: Сводный отчет эксперта РАО, сгенерированный ML-пайплайном, содержащий ключевые выводы и рекомендации по экспертизе. 
            Структура отчета может включать различные разделы, такие как анализ документов, выявленные риски, рекомендации по улучшению и т.д., в зависимости от логики пайплайна. 
            Если send_to_external установлен в True, результат также будет отправлен во внешнюю систему, и в ответе может быть указано подтверждение отправки.

    '''
    try:
        expertise_id = request.expertise_id
        send_to_external = request.send_to_external
        
        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")
        
        # Запускаем пайплайн
        results = await rao_conclusion(expertise_id, x_api_database, http_manager, send_to_external)

        return results
        
    except Exception as e:
        logger.error(f"Ошибка при создании сводного отчета эксперта РАО: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при создании сводного отчета эксперта РАО: {str(e)}")
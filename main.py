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
from configs.llm_client import get_llm
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Header, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from typing import List, Optional, Dict
from celery.result import AsyncResult

from configs.config import Config
from configs.schemas import EISParseRequest, EvaluateRequest, ExpertsScoringRequest, RAOConclusionRequest, ViolationsReportRequest
from configs.eis_parsing import EISParser
from configs.utils import APIKeyMiddleware
from configs.working_with_db import get_contract_info_from_db
from configs.logger import setup_logging, get_logger
from src.law_detector import LawDetector
from src.rao_conclusion import rao_conclusion
from src.rating import rating
from src.scoring import scoring
from tasks import evaluate_documents_task

# main.py
from configs.http_client_manager import HTTPClientManager
from src.violations_reporter import ViolationsReporter

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
    Обработчик API-запроса для получения ключевых реквизитов контракта по идентификатору закупки.

    Извлекает из базы данных номер, сумму и дату контракта на основе данных, ранее сохранённых
    в поле `contract_draft` таблицы `raw_document_data`. Возвращает информацию в виде JSON.
    Используется внешними сервисами для быстрого доступа к основным параметрам контракта.

    Args:
        request_body (Dict[str, str], optional): Тело запроса в формате JSON с обязательным полем `procurement_id`.
            Пример: {"procurement_id": "12345"}.

    Raises:
        HTTPException: Если поле `procurement_id` отсутствует в запросе — возвращает ошибку 400.

    Returns:
        dict: Словарь с реквизитами контракта:
            - contract_number (str): Номер контракта или "0", если не найден.
            - amount (str): Сумма контракта в виде строки или "0", если не найдена.
            - date (str): Дата контракта в текстовом формате или "0", если не найдена.
            При возникновении внутренней ошибки возвращается тот же словарь со значениями "0".
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
        dict: Словарь, где ключ — идентификатор эксперта, значение — рейтинг.
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
    Эндпоинт для получения сводного отчета эксперта РАО по результатам экспертизы.
    Принимает идентификатор экспертизы и флаг отправки отчета во внешний сервис.
    Инициализирует пайплайн генерации заключения и возвращает результат.
    Args:
        request (RAOConclusionRequest): Тело запроса, содержащее:
            - expertise_id (int): Идентификатор экспертизы для анализа.
            - send_to_external (bool): Флаг, указывающий, нужно ли отправлять отчет во внешний сервис.
        x_api_database (str): Заголовок с указанием среды базы данных ('dev', 'prod', 'stage').
    Returns:
        Dict[str, Any]: Словарь с результатами анализа, содержащий:
            - status (str): Статус выполнения ('success' или 'error').
            - results (List[DocumentContentResponse]): Список детальных результатов анализа каждого документа.
            - errors (List[str]): Список сообщений об ошибках, произошедших при обработке отдельных файлов (если есть).
    Raises:
        HTTPException:
            - 400: Если поле 'expertise_id' в запросе отсутствует.
            - 500: Если произошла внутренняя ошибка при генерации отчета.
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

@app.post("/get-violations-report")
async def get_violations_report(request: ViolationsReportRequest):
    '''
    Эндпоинт для формирования аналитического отчета о нарушениях на основе переданных данных.
    
    Принимает на вход метрику, фильтры, сырые данные и список доступных типов графиков.
    Инициализирует LLM-клиент и передает управление в пайплайн генерации отчета.
    
    Args:
        request (ViolationsReportRequest): Тело запроса, содержащее:
            - metric (str): Название анализируемой метрики.
            - filters (Dict[str, Any]): Словарь с примененными фильтрами.
            - data (Any): Сырые данные для анализа (словарь, список или JSON-строка).
            - charts (List[str]): Список доступных типов графиков для выбора LLM.
            
    Returns:
        Dict[str, Any]: Словарь с результатами анализа, содержащий:
            - status (str): Статус выполнения ('success' или 'error').
            - raw_text (str): Сгенерированный аналитический текст.
            - chart_type (str): Рекомендуемый тип графика.
            - chart_title (str): Заголовок для рекомендуемого графика.
            
    Raises:
        HTTPException: 
            - 400: Если поле 'data' в запросе пустое или отсутствует.
            - 500: Если произошла внутренняя ошибка при генерации отчета.
    '''
    try:
        metric = request.metric
        filters = request.filters
        data = request.data
        charts = request.charts
        
        if not data:
            raise HTTPException(status_code=400, detail="Поле 'data' обязательно")
        
        llm_client, llm_model = get_llm()
        
        # Запускаем пайплайн
        reporter = ViolationsReporter(llm_client, llm_model)
        report = await reporter.analize_data_from_content(metric, filters, data, charts)

        return report
        
    except Exception as e:
        logger.error(f"Ошибка при создании аналитического отчета: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при создании аналитического отчета: {str(e)}")


@app.post("/parse-eis")
async def parse_eis(request: EISParseRequest):
    request_method = request.request_method
    subsystem_type = request.subsystem_type
    reg_number = request.reg_number
    org_region = request.org_region
    fz = request.fz
    document_type = request.document_type
    nsi_code = request.nsi_code
    nsi_kind = request.nsi_kind
    exact_date = request.exact_date
    procurement_id = request.procurement_id

    if not request_method:
        raise HTTPException(status_code=400, detail="Поле 'request_method' обязательно")    
    if request_method == "getDocsByReestrNumberRequest":
        if not reg_number:
            raise HTTPException(status_code=400, detail="Поле 'reg_number' обязательно")    
    elif request_method == "getDocsByOrgRegionRequest":
        if not org_region:
            raise HTTPException(status_code=400, detail="Поле 'org_region' обязательно")
        if not exact_date:
            raise HTTPException(status_code=400, detail="Поле 'exact_date' обязательно")
    elif request_method == "getNsiRequest":
        pass
    else:
        raise HTTPException(status_code=400, detail="Неверный метод запроса 'request_method'")
    
    cloud_parser = EISParser(http_manager)
    result = await cloud_parser._parse_eis_soap_ip(request_method, subsystem_type, reg_number, org_region, fz, document_type, nsi_code, nsi_kind, exact_date, procurement_id)

    
    # === Обработка ошибок от парсера ===
    if result.get("status") == "error":
        # Создаём ZIP с файлом ошибки для единообразия формата
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            error_metadata = {
                "error": result.get("error"),
                "request_method": request_method,
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "eis_response": result.get("eis_response")  # Если есть
            }
            zip_file.writestr("ERROR.json", json.dumps(error_metadata, ensure_ascii=False, indent=2))
        
        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="eis_error_{request_method}.zip"',
                "X-Response-Status": "error"  # Заголовок для быстрой проверки
            },
            status_code=status.HTTP_200_OK  # 200, т.к. это валидный ответ с ошибкой внутри
        )
    
    # === Успешный ответ: создаём ZIP с файлами + metadata ===
    existing_names = set()

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        
        # 1. Добавляем файлы из результата
        for file_info in result.get("files", []):
            content = None
            
            # Получаем контент: из base64 или с диска
            if "content_base64" in file_info and file_info["content_base64"]:
                content = base64.b64decode(file_info["content_base64"])
            elif "file_path" in file_info and file_info["file_path"]:
                if os.path.exists(file_info["file_path"]):
                    with open(file_info["file_path"], 'rb') as f:
                        content = f.read()
                    # 🧹 Опционально: удаляем временный файл
                    # os.unlink(file_info["file_path"])
            
            if content is not None:
                # zip_file.writestr(file_info["filename"], content)
                # logger.info(f"Добавлен файл в архив: {file_info['filename']}")
                if file_info["status"] == "success":
                    unique_name = cloud_parser._make_unique_filename(file_info["filename"], existing_names)
                    # zip_file.write(file_info["file_path"], arcname=unique_name)
                    zip_file.writestr(unique_name, content)
                    logger.info(f"Добавлен файл в архив: {unique_name}")
                    # Не забудьте удалить временный файл:
                    os.unlink(file_info["file_path"])
        
        # 2. Добавляем metadata.json с response_content и информацией о запросе
        metadata = {
            "status": "success",
            "request": {
                "method": request_method,
                "reg_number": reg_number,
                "org_region": org_region,
                "document_type": document_type,
                "procurement_id": procurement_id
            },
            "result": {
                "file_count": result.get("file_count", 0),
                "archive_count": result.get("archive_count", 1),
                "source": result.get("source")
            },
            "eis_response": result.get("eis_response"),  # Полное parsed SOAP response
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        zip_file.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    
    zip_buffer.seek(0)
    
    # === Возвращаем StreamingResponse ===
    filename_safe = re.sub(r'[<>:"/\\|?*]', '_', reg_number or request_method)
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="eis_files_{filename_safe}.zip"',
            "X-Response-Status": "success",
            "X-File-Count": str(result.get("file_count", 0))
        }
    )


@app.post("/get-law")
async def get_law(
    request_body: Dict[str, int] = Body(...)
):
    """

    """
    try:
        expertise_id = request_body.get("expertise_id")
        
        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")
        
        # Запускаем скоринг пайплайн

        law_detector = LawDetector()
        result = await law_detector.get_law(expertise_id)
        
        return result
        
    except Exception as e:
        logger.error(f"Ошибка при определении закона: {str(e)}", exc_info=True)
        return {"expertise_id": expertise_id, "law": 0}
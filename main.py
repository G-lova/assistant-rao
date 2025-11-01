import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict

from configs.utils import (APIKeyMiddleware,
                           read_file,
                           create_summary_report)
from configs.working_with_db import (save_raw_data,
                                     save_clean_conclusion,
                                     get_contract_info_from_db,
                                     save_summary_report)
from src.evaluator import (analyze_document_chunks,
                           split_large_text,
                           check_completeness_with_ai,
                           create_unprocessed_document_analysis)
from src.scoring import scoring
from src.rating import rating
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG
from tasks import evaluate_documents_task
from celery.result import AsyncResult


app = FastAPI(debug=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(APIKeyMiddleware)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Глобальный словарь для хранения результатов анализа документов
document_analysis_cache = {}


@app.post("/evaluate-documents")
async def evaluate_documents_batch(
    procurement_id: str = Form(...),
    expertise_customer: Optional[str] = Form(None),
    files: List[UploadFile] = File(None),
    links: List[str] = Form(None),
    eis_links: Optional[str] = Form(None),
    legislation: Optional[str] = Form("44-ФЗ"),
    procurement_method: Optional[str] = Form("Конкурс"),
    expertise_details: Optional[str] = Form("Полный комплект документов о закупке")
):
    if not files and not (links or eis_links):
        raise HTTPException(status_code=400, detail="Необходимо предоставить либо файлы, либо ссылки")

    file_paths = []
    filenames = []

    # Сохраняем загруженные файлы во временные пути
    if files:
        for file in files:
            suffix = os.path.splitext(file.filename)[1] or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await file.read()
                if not content:
                    raise HTTPException(status_code=400, detail=f"Файл {file.filename} пустой")
                tmp.write(content)
                file_paths.append(tmp.name)
                filenames.append(file.filename)

    # Запускаем Celery-задачу
    task = evaluate_documents_task.delay(
        procurement_id=procurement_id,
        expertise_customer=expertise_customer,
        file_paths=file_paths,
        filenames=filenames,
        links=links,
        eis_links=eis_links,
        legislation=legislation,
        procurement_method=procurement_method,
        expertise_details=expertise_details
    )

    return {
        "task_id": task.id,
        "status": "processing",
        "message": "Задача по обработке документов запущена"
    }


@app.get("/task/{task_id}")
async def get_task_status(task_id: str):
    task = AsyncResult(task_id)
    if task.state == 'PENDING':
        return {"status": "pending"}
    elif task.state == 'FAILURE':
        return {"status": "failed", "error": str(task.info)}
    elif task.state == 'SUCCESS':
        return {"status": "completed", "result": task.result}
    else:
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

    if not procurement_id:
        raise HTTPException(status_code=400, detail="Поле 'procurement_id' обязательно в теле запроса")

    try:
        info = get_contract_info_from_db(procurement_id)
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
    Подбирает список идентификаторов экспертов для заданной экспертизы с использованием скоринговой модели.

    Принимает идентификатор экспертизы, запускает ML-пайплайн скоринга и возвращает
    отсортированный список ID экспертов, наиболее подходящих для проведения экспертизы.
    В случае временных сбоев автоматически повторяет запрос согласно настройкам EXERTS_RETRY_CONFIG.

    Args:
        request_body (Dict[str, int], optional): Тело запроса в формате JSON,
            содержащее обязательное поле "expertise_id" — числовой идентификатор экспертизы.

    Raises:
        HTTPException: С кодом 400, если не указан expertise_id.
        HTTPException: С кодом 500, если произошла ошибка при выполнении скоринга.

    Returns:
        _type_: Список целых чисел — идентификаторов экспертов, отобранных моделью.
    """
    try:
        expertise_id = request_body.get("expertise_id")
        
        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")
        
        # Запускаем скоринг пайплайн
        results = scoring(expertise_id, x_api_database)
        
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
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Возвращает рейтинги экспертов на основе SQL-запроса `experts_rating.sql`.

    Выполняет SQL-запрос через внешний API (в зависимости от окружения, указанного в X-API-Database)
    и возвращает словарь { expert_id: rating }.

    Args:
        request_body (Dict[str, str]): JSON с параметрами (если нужны; здесь не используются).
        x_api_database (str): Заголовок с указанием среды ('dev', 'prod' и т.д.).

    Returns:
        dict: Словарь, где ключ — идентификатор эксперта, значение — рейтинг.
    """
    try:
        results = rating()

        logger.info(f"Успешно получено {len(results)} рейтингов экспертов (DB: {x_api_database})")

        return results

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
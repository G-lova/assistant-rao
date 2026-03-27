import os
import tempfile
import logging
import json

from configs.schemas import ExpertsScoringRequest, RAOConclusionRequest
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict
from celery.result import AsyncResult

from configs.utils import APIKeyMiddleware
from configs.working_with_db import get_contract_info_from_db
from src.scoring import scoring
from src.rating import rating
from tasks import evaluate_documents_task
from configs.procurement_requirements import DOCUMENT_CODE_TO_LABEL
from src.rao_conclusion import rao_conclusion

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

@app.post("/rao_conclusion")
async def get_rao_conclusion(
    request: RAOConclusionRequest,
    x_api_database: str = Header(default="dev", alias="X-API-Database")
    ):
    '''
    Docstring for get_rao_conclusion
    
    :param request_body: Description
    :type request_body: Dict[str, int]
    :param send_to_external: Description
    :type send_to_external: bool
    :param x_api_database: Description
    :type x_api_database: str
    '''
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


@app.post("/evaluate-documents")
async def evaluate_documents_batch(
    procurement_id: str = Form(...),
    type: str = Form(...),
    checkType2: str = Form(...),
    object: str = Form(...),
    users_organization: str = Form(..., alias="users.organization"),
    linkDocs: Optional[str] = Form(None),
    media_json: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=None)
):
    # === Парсинг media_json ===
    if not media_json or media_json.strip() == "":
        media = []
    else:
        try:
            media = json.loads(media_json)
            if not isinstance(media, list):
                raise ValueError("media_json должен быть массивом объектов")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Некорректный JSON в media: {e}")

    # === Списки для передачи в задачу ===
    all_file_paths = []
    all_filenames = []
    all_document_codes = []
    all_document_labels = []
    all_comments = []
    all_links = []
    link_to_metadata = {}  # ссылка → (code, label, comment)

    # === Обработка media_json: собираем метаданные и ссылки ===
    for item in media:
        code = item.get("code")
        if not code:
            continue
        label = DOCUMENT_CODE_TO_LABEL.get(code, "Неизвестный документ")
        comment = item.get("comment")
        links_list = item.get("links", [])
        files_list = item.get("files", [])  # ссылки как файлы

        # Сохраняем метаданные для КАЖДОЙ ссылки
        for url in links_list + files_list:
            link_to_metadata[url] = (code, label, comment)

        all_links.extend(links_list + files_list)

    # === Обработка загруженных файлов ===
    if files:
        for file in files:
            if not file.filename:
                continue
            if "___" in file.filename:
                doc_code, orig_name = file.filename.split("___", 1)
            else:
                doc_code = "unknown"
                orig_name = file.filename

            # Получаем метаданные из media_json по коду, если есть
            label = DOCUMENT_CODE_TO_LABEL.get(doc_code, "Неизвестный документ")
            comment = None  # в текущей схеме комментарий к файлу не передаётся, только к ссылке
            # Если вы хотите комментарий к файлу — добавьте соглашение, но пока оставим None

            suffix = os.path.splitext(orig_name)[1] or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await file.read()
                if not content:
                    raise HTTPException(status_code=400, detail=f"Файл {file.filename} пустой")
                tmp.write(content)
                all_file_paths.append(tmp.name)
                all_filenames.append(orig_name)
                all_document_codes.append(doc_code)
                all_document_labels.append(label)
                all_comments.append(comment)

    # Добавляем linkDocs в all_links (но без метаданных — он для ЕИС)
    if linkDocs and linkDocs.strip():
        all_links.append(linkDocs)

    # === Запуск задачи ===
    task = evaluate_documents_task.delay(
        procurement_id=procurement_id,
        expertise_customer=users_organization,
        file_paths=all_file_paths,
        filenames=all_filenames,
        document_codes=all_document_codes,
        document_labels=all_document_labels,
        comments=all_comments,
        links=all_links,
        eis_links=linkDocs,
        link_to_metadata=link_to_metadata,
        legislation=type,
        procurement_method=checkType2,
        expertise_details="Полный комплект документов о закупке"
    )

    return {
        "task_id": task.id,
        "status": "processing",
        "message": "Задача запущена"
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
        results = await scoring(expertise_id, details, x_api_database)

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

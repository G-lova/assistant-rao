import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from configs.schemas import DocumentContentResponse
from configs.utils import APIKeyMiddleware, read_file
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from configs.working_with_db import save_document_content_to_db, ALLOWED_COLUMNS
from src.evaluator import check_documents


app = FastAPI()

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


@app.post("/evaluate-documents", response_model=DocumentContentResponse)
async def evaluate_documents(
    procurement_id: str = Form(...),
    document_type: str = Form(...),
    legislation: str = Form(...),
    procurement_method: str = Form(...),
    expertise_details: str = Form(...),
    file: UploadFile = File(...)
):
    """
    Оценивает загруженный документ на соответствие типу, содержанию и требованиям закупки.

    Эндпоинт принимает файл и метаданные, извлекает текст, проверяет соответствие ожидаемому
    типу документа и сохраняет результат в базу данных при успешной валидации.
    Используется для автоматической экспертизы документов закупок.

    Args:
        procurement_id (str, optional): Уникальный идентификатор закупки.
        document_type (str, optional): Ожидаемый тип документа (например, "Извещение").
        legislation (str, optional): Тип законодательства (например, "44-ФЗ").
        procurement_method (str, optional): Способ закупки (например, "Конкурс").
        expertise_details (str, optional): Детали экспертизы, определяющие набор требований.
        file (UploadFile, optional): Загруженный файл документа (PDF, DOCX, XLSX и др.).

    Returns:
        DocumentContentResponse: Объект с результатами обработки, включающий:
            - procurement_id, document_type, filename, content_type, size — метаданные;
            - content — краткое содержание (первые 100 символов) или сообщение об ошибке;
            - is_valid — статус валидации (соответствует/не соответствует).
            В случае ошибки возвращается ответ с is_valid=False и описанием проблемы.
    """
    try:
        suffix = os.path.splitext(file.filename)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # Логируем имя и размер файла
            logger.info(f"Обработка файла: {file.filename}, размер: {len(content)} байт, тип: {file.content_type}")

            # Извлечение текста
            extracted_text = read_file(tmp_path, original_filename=file.filename)
            logger.info(f"Извлечённый текст из {file.filename} (первые 500 символов):\n{extracted_text[:500]}")

            # Запуск анализа
            result = await check_documents(
                file_paths=[tmp_path],
                original_filenames=[file.filename],
                legislation=legislation,
                procurement_method=procurement_method,
                expertise_details=expertise_details
            )

            # Логируем результат анализа
            logger.info(f"Результат анализа {file.filename}: {json.dumps(result, ensure_ascii=False, indent=2)}")

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if "error" in result:
            short_content = extracted_text[:100]
            is_valid = False
            response_content = f"Неверный документ: {short_content}"
            logger.warning(f"Ошибка анализа {file.filename}: {result['error']}")
        else:
            analysis = next((da for da in result.get("document_analysis", []) if file.filename in da.get("document_name", "")), None)
            short_content = extracted_text[:100]
            is_valid = analysis and analysis.get("type_compliance", {}).get("status") == "соответствует"
            response_content = short_content if is_valid else f"Неверный документ: {short_content}"

            # Логируем статус соответствия
            status = analysis.get("type_compliance", {}).get("status", "неизвестно")
            logger.info(f"Статус соответствия для {file.filename}: {status}")

            col_name = DOCUMENT_TYPE_MAPPING.get(document_type)
            if col_name and col_name in ALLOWED_COLUMNS:
                # Логируем сохранение в БД
                logger.info(f"Сохранение в БД: procurement_id={procurement_id}, колонка={col_name}, длина текста={len(extracted_text)}")
                save_document_content_to_db(
                    procurement_id=procurement_id,
                    document_type=col_name,
                    content=extracted_text
                )

        return DocumentContentResponse(
            procurement_id=procurement_id,
            document_type=document_type,
            filename=file.filename,
            content_type=file.content_type or "unknown",
            content=response_content,
            size=file.size,
            is_valid=is_valid
        )

    except Exception as e:
        logger.error(f"Критическая ошибка при анализе {file.filename}: {str(e)}", exc_info=True)
        return DocumentContentResponse(
            procurement_id=procurement_id,
            document_type=document_type,
            filename=file.filename,
            content_type=file.content_type or "unknown",
            content="Неверный документ: Ошибка анализа",
            size=file.size,
            is_valid=False
        )
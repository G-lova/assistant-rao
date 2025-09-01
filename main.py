import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from configs.schemas import DocumentContentResponse
from configs.utils import APIKeyMiddleware, read_file
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.evaluator import check_documents
from src.evaluator import analyze_single_document
from configs.working_with_db import save_raw_data, save_clean_conclusion


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

    Эндпоинт принимает файл и метаданные, извлекает текст через OCR, анализирует документ
    и сохраняет результат в две таблицы БД:
    - raw_document_data: извлечённые структурированные данные (даты, суммы, юрлица)
    - clean_document_conclusions: итоговое заключение модели

    Args:
        procurement_id (str): Уникальный идентификатор закупки.
        document_type (str): Ожидаемый тип документа (например, "Проект договора").
        legislation (str): Тип законодательства (например, "44-ФЗ").
        procurement_method (str): Способ закупки (например, "Конкурс").
        expertise_details (str): Детали экспертизы.
        file (UploadFile): Загруженный файл (PDF, DOCX и др.).

    Returns:
        DocumentContentResponse: Результат с метаданными, фрагментом текста и статусом.
    """
    try:
        suffix = os.path.splitext(file.filename)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            logger.info(f"Обработка файла: {file.filename}, размер: {len(content)} байт")

            # Извлечение текста (все PDF проходят через OCR)
            extracted_text = read_file(tmp_path, original_filename=file.filename)
            if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                raise ValueError("Не удалось извлечь текст из документа")

            logger.info(f"Извлечено {len(extracted_text)} символов из {file.filename}")

            # Анализ документа (возвращает результат с raw_data и conclusion)
            result = await analyze_single_document(
                content=extracted_text,
                document_name=file.filename,
                document_type=document_type,
                law_type=legislation,
                procurement_method=procurement_method
            )

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # Обработка результата
        if result["status"] == "error":
            short_content = extracted_text[:300]
            is_valid = False
            response_content = f"Неверный документ: {short_content}"
            logger.warning(f"Ошибка анализа {file.filename}: {result['analysis']['conclusion']}")
        else:
            analysis = result["analysis"]
            type_compliance = analysis["type_compliance"]
            
            # Проверяем соответствие типа документа
            is_valid = (type_compliance["status"] == "соответствует" and 
                       type_compliance.get("confidence", 0) >= 0.7)
            
            short_content = extracted_text[:300]
            
            if not is_valid:
                expected = type_compliance.get("expected_type", document_type)
                actual = type_compliance.get("actual_type", "неизвестно")
                confidence = type_compliance.get("confidence", 0)
                
                response_content = f"НЕСООТВЕТСТВИЕ ТИПА: Ожидался '{expected}', получен '{actual}' (уверенность: {confidence:.2f})\n\n{short_content}"
                
                # Обновляем заключение
                analysis["conclusion"] = f"Документ не соответствует заявленному типу. {analysis['conclusion']}"
            else:
                response_content = short_content

            # Сохранение в БД
            try:
                # 1. Сохраняем сырые данные (извлечённые сущности)
                save_raw_data(
                    procurement_id=procurement_id,
                    document_type=document_type,
                    full_analysis=analysis,
                )

                # 2. Сохраняем чистое заключение
                save_clean_conclusion(
                    procurement_id=procurement_id,
                    document_type=document_type,
                    conclusion=analysis["conclusion"]
                )

                logger.info(f"Успешно сохранены raw и clean данные для {document_type} (закупка: {procurement_id})")

            except Exception as db_error:
                logger.error(f"Ошибка сохранения в БД: {str(db_error)}", exc_info=True)
                # Не прерываем процесс — продолжаем с ответом, но помечаем предупреждение
                response_content += " [Предупреждение: не удалось сохранить в БД]"

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
            is_valid=False,
            error=str(e)
        )
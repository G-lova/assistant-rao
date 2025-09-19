import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from configs.schemas import DocumentContentResponse
from configs.utils import APIKeyMiddleware, read_file
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.evaluator import check_consistency, analyze_single_document
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
    Обработчик POST-запроса для анализа и валидации загруженного документа.

    Принимает файл и метаданные от клиента, извлекает текст, определяет реальный тип документа
    с помощью языковой модели, сравнивает его с ожидаемым типом и сохраняет результаты.
    Поддерживает проверку согласованности между документами одной закупки.
    Возвращает структурированный ответ с признаком валидности и фрагментом содержания.

    Args:
        procurement_id (str): Уникальный идентификатор закупки, к которой относится документ.
        document_type (str): Ожидаемый тип документа (указывается пользователем в интерфейсе).
        legislation (str): Нормативная база (например, "44-ФЗ"), используемая для контекстной оценки.
        procurement_method (str): Способ проведения закупки (например, "Конкурс", "Аукцион").
        expertise_details (str): Дополнительные сведения об экспертизе (зарезервировано для будущего использования).
        file (UploadFile): Загруженный файл для анализа.

    Raises:
        ValueError: Если не удаётся извлечь читаемый текст из файла.

    Returns:
        DocumentContentResponse: Объект с информацией о результате обработки, содержащий:
            - procurement_id — идентификатор закупки,
            - document_type — нормализованный тип документа (по результатам анализа),
            - filename — оригинальное имя файла,
            - content_type — MIME-тип файла,
            - content — первые 300 символов содержания или сообщение об ошибке,
            - size — размер файла в байтах,
            - is_valid — признак соответствия типа документа ожидаемому,
            - error — описание ошибки, если возникла (необязательное поле).
    """
    try:
        suffix = os.path.splitext(file.filename)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            logger.info(f"Обработка файла: {file.filename}")

            extracted_text = read_file(tmp_path, original_filename=file.filename)
            if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                raise ValueError("Не удалось извлечь текст")

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

        if result["status"] == "error":
            short_content = extracted_text[:300]
            is_valid = False
            response_content = f"Ошибка анализа: {short_content}"
        else:
            analysis = result["analysis"]
            actual_type_from_model = analysis["type_compliance"].get("actual_type", "").strip()

            # Используем тип от модели!
            from configs.utils import normalize_document_type
            final_doc_type = normalize_document_type(actual_type_from_model)

            # Обновляем результат
            analysis["type_compliance"]["actual_type"] = final_doc_type
            analysis["type_compliance"]["expected_type"] = document_type

            # Пересчитываем статус
            confidence = analysis["type_compliance"].get("confidence", 0)
            is_valid = (
                final_doc_type != "Дополнительные материалы"
                and confidence >= 0.7
            )
            analysis["type_compliance"]["status"] = "соответствует" if is_valid else "не соответствует"

            if not is_valid:
                response_content = (
                    f"НЕСООТВЕТСТВИЕ ТИПА: Определён как '{final_doc_type}', ожидался '{document_type}'\n\n"
                    + extracted_text[:300]
                )
                analysis["conclusion"] = f"Тип документа не подтверждён. {analysis['conclusion']}"
            else:
                response_content = extracted_text[:300]

            try:
                save_raw_data(
                    procurement_id=procurement_id,
                    document_type=final_doc_type,
                    full_analysis=analysis
                )

                save_clean_conclusion(
                    procurement_id=procurement_id,
                    document_type=final_doc_type,
                    conclusion=analysis["conclusion"]
                )

                # Проверка согласованности между документами
                consistency_result = check_consistency(procurement_id)
                save_clean_conclusion(
                    procurement_id=procurement_id,
                    document_type="consistency_check",
                    conclusion=consistency_result["conclusion"]
                )

                logger.info(f"Сохранено: procurement_id={procurement_id}, type={final_doc_type}")

            except Exception as db_error:
                logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                response_content += " [Ошибка сохранения в БД]"

        # В ответе тоже возвращаем правильный тип
        return DocumentContentResponse(
            procurement_id=procurement_id,
            document_type=final_doc_type,
            filename=file.filename,
            content_type=file.content_type or "unknown",
            content=response_content,
            size=file.size,
            is_valid=is_valid
        )

    except Exception as e:
        logger.error(f"Критическая ошибка: {str(e)}", exc_info=True)
        return DocumentContentResponse(
            procurement_id=procurement_id,
            document_type="Дополнительные материалы",
            filename=file.filename,
            content_type="unknown",
            content="Ошибка анализа",
            size=file.size,
            is_valid=False,
            error=str(e)
        )
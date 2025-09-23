import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict

from configs.schemas import DocumentContentResponse, BatchDocumentResponse
from configs.utils import APIKeyMiddleware, normalize_document_type, read_file
from src.evaluator import check_consistency, analyze_single_document
from configs.working_with_db import save_raw_data, save_clean_conclusion, get_contract_info_from_db


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


@app.post("/evaluate-documents")
async def evaluate_documents_batch(
    procurement_id: str = Form(...),
    files: List[UploadFile] = File(..., description="Список загружаемых документов"),
    legislation: Optional[str] = Form("44-ФЗ"),
    procurement_method: Optional[str] = Form("Конкурс"),
    expertise_details: Optional[str] = Form("Полный комплект документов о закупке")
):
    """
    Обработчик POST-запроса для массовой оценки документов по одной закупке.

    Принимает несколько файлов, последовательно обрабатывает каждый: извлекает текст,
    определяет тип документа с помощью языковой модели, проверяет соответствие и сохраняет
    результаты в базу данных. Возвращает список структурированных ответов с информацией
    о каждом документе, включая признак валидности и фрагмент содержания.

    Args:
        procurement_id (str): Уникальный идентификатор закупки, к которой относятся документы.
        files (List[UploadFile]): Список загруженных файлов для анализа (PDF, DOCX, XLSX и др.).
        legislation (Optional[str], optional): Нормативная база (например, "44-ФЗ"). По умолчанию — "44-ФЗ".
        procurement_method (Optional[str], optional): Способ проведения закупки (например, "Конкурс"). 
            Используется как контекст для LLM. По умолчанию — "Конкурс".
        expertise_details (Optional[str], optional): Дополнительные сведения о цели экспертизы. 
            Зарезервировано для будущего расширения. По умолчанию — "Полный комплект документов о закупке".

    Raises:
        ValueError: Если файл пустой или не удаётся извлечь из него читаемый текст.
        Exception: При ошибках обработки отдельного файла — продолжает работу с остальными.

    Returns:
        List[DocumentContentResponse]: Список объектов с результатами анализа каждого документа.
            Каждый элемент содержит:
            - procurement_id — идентификатор закупки,
            - document_type — нормализованный тип документа (по результатам анализа),
            - filename — оригинальное имя файла,
            - content_type — MIME-тип,
            - content — первые 300 символов содержания или сообщение об ошибке,
            - size — размер файла,
            - is_valid — признак соответствия типа документа ожидаемому,
            - error — описание ошибки (если возникла).
    """
    results = []

    for file in files:
        try:
            # Создаём временный файл
            suffix = os.path.splitext(file.filename)[1] or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await file.read()
                if not content:
                    raise ValueError("Файл пустой")
                tmp.write(content)
                tmp_path = tmp.name

            try:
                logger.info(f"Обработка файла: {file.filename} для закупки {procurement_id}")

                # Извлечение текста
                extracted_text = read_file(tmp_path, original_filename=file.filename)

                if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                    raise ValueError("Не удалось извлечь текст из файла")

                # Анализ через LLM
                analysis_result = await analyze_single_document(
                    content=extracted_text,
                    document_name=file.filename,
                    document_type="",  # не передаём — модель сама определит тип
                    law_type=legislation,
                    procurement_method=procurement_method
                )

                if analysis_result["status"] == "error":
                    final_doc_type = "Дополнительные материалы"
                    is_valid = False
                    response_content = f"Ошибка анализа: {extracted_text[:300]}"
                else:
                    analysis = analysis_result["analysis"]
                    actual_type_from_model = analysis["type_compliance"].get("actual_type", "").strip()

                    # Нормализуем тип
                    final_doc_type = normalize_document_type(actual_type_from_model)

                    confidence = analysis["type_compliance"].get("confidence", 0)
                    is_valid = (
                        final_doc_type != "Дополнительные материалы"
                        and confidence >= 0.7
                    )
                    analysis["type_compliance"]["status"] = "соответствует" if is_valid else "не соответствует"

                    if not is_valid:
                        response_content = (
                            f"НЕСООТВЕТСТВИЕ ТИПА: Определён как '{final_doc_type}'\n\n"
                            + extracted_text[:300]
                        )
                        analysis["conclusion"] = f"Тип документа не подтверждён. {analysis['conclusion']}"
                    else:
                        response_content = extracted_text[:300]

                    # Сохраняем результаты
                    try:
                        save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                        save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=analysis["conclusion"])

                        # Проверка согласованности (один раз на закупку, но можно вызывать каждый раз — idempotent)
                        consistency_result = check_consistency(procurement_id)
                        save_clean_conclusion(
                            procurement_id=procurement_id,
                            document_type="consistency_check",
                            conclusion=consistency_result["conclusion"]
                        )

                        logger.info(f"Сохранено: {procurement_id}, {final_doc_type}")
                    except Exception as db_error:
                        logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                        response_content += " [Ошибка сохранения в БД]"

            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            # Формируем ответ
            result_response = DocumentContentResponse(
                procurement_id=procurement_id,
                document_type=final_doc_type,
                filename=file.filename,
                content_type=file.content_type or "application/octet-stream",
                content=response_content,
                size=file.size,
                is_valid=is_valid
            )
            results.append(result_response)

        except Exception as e:
            logger.error(f"Ошибка при обработке {file.filename}: {str(e)}", exc_info=True)
            results.append(
                DocumentContentResponse(
                    procurement_id=procurement_id,
                    document_type="Дополнительные материалы",
                    filename=file.filename,
                    content_type="unknown",
                    content=f"Критическая ошибка: {str(e)}",
                    size=file.size,
                    is_valid=False,
                    error=str(e)
                )
            )

    return results


@app.post("/get-contract-info")
async def api_get_contract_info(request_body: Dict[str, str] = Body(...)):
    """
    Обработчик API-запроса для получения реквизитов контракта по идентификатору закупки.

    Принимает JSON с полем `procurement_id`, извлекает данные о номере, сумме и дате контракта
    из базы данных (из поля `contract_draft`) и возвращает их клиенту. Используется для интеграции
    с внешними системами, которым требуется быстрый доступ к основным параметрам контракта.

    Args:
        request_body (Dict[str, str], optional): Тело запроса в формате JSON, должно содержать ключ "procurement_id".
            Пример: {"procurement_id": "12345"}.

    Raises:
        HTTPException: Если в теле запроса отсутствует обязательное поле `procurement_id` — возвращает ошибку 400.

    Returns:
        dict: Словарь с реквизитами контракта:
            - contract_number (str): Номер контракта или "0", если не найден.
            - amount (str): Сумма контракта (в виде строки) или "0", если не найдена.
            - date (str): Дата контракта в текстовом формате или "0", если не найдена.
            При внутренней ошибке возвращается тот же словарь со значениями "0".
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
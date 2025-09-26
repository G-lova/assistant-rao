import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict

from configs.schemas import DocumentContentResponse, BatchDocumentResponse
from configs.utils import APIKeyMiddleware, normalize_document_type, read_file, check_procurement_completeness
from src.evaluator import check_documents_consistency, analyze_document_chunks, split_large_text
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


# Глобальный словарь для хранения результатов анализа документов
document_analysis_cache = {}


@app.post("/evaluate-documents")
async def evaluate_documents_batch(
    procurement_id: str = Form(...),
    files: List[UploadFile] = File(..., description="Список загружаемых документов"),
    legislation: Optional[str] = Form("44-ФЗ"),
    procurement_method: Optional[str] = Form("Конкурс"),
    expertise_details: Optional[str] = Form("Полный комплект документов о закупке")
):
    """
    Обработчик POST-запроса для массовой проверки документов закупки.

    Принимает несколько файлов и метаданные (ID закупки, законодательство и т.д.), 
    последовательно анализирует каждый документ с автоматическим определением его типа,
    проверяет полноту комплекта и согласованность данных между документами.
    Поддерживает обработку больших файлов через разбиение на фрагменты.

    Args:
        procurement_id (str): Уникальный идентификатор закупки для группировки документов.
        files (List[UploadFile]): Список загруженных файлов (PDF, DOCX, XLSX и др.).
        legislation (Optional[str], optional): Нормативная база (например, "44-ФЗ"). По умолчанию — "44-ФЗ".
        procurement_method (Optional[str], optional): Способ проведения закупки. По умолчанию — "Конкурс".
        expertise_details (Optional[str], optional): Дополнительные сведения об экспертизе. 
            По умолчанию — "Полный комплект документов о закупке".

    Raises:
        ValueError: Если файл пустой или не удаётся извлечь текст.
        Exception: При критических ошибках чтения или обработки файла.

    Returns:
        dict: Структурированный ответ, содержащий:
            - procurement_id: ID закупки.
            - documents: Список результатов по каждому документу (тип, валидность, вывод).
            - overall_conclusion: Итоговое заключение по всем документам.
            - completeness_summary: Информация о полноте комплекта.
            - consistency_summary: Результаты проверки согласованности данных.
            Все промежуточные результаты сохраняются в БД и кэшируются.
    """
    global document_analysis_cache
    
    documents_results = []
    document_analysis_results = {}
    files_dict = {}

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
                files_dict[file.filename] = tmp_path

            try:
                logger.info(f"Обработка файла: {file.filename} для закупки {procurement_id}")

                # Извлечение текста
                extracted_text = read_file(tmp_path, original_filename=file.filename)

                if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                    raise ValueError("Не удалось извлечь текст из файла")

                # Разделяем большой текст на блоки
                chunks = split_large_text(extracted_text, max_chunk_size=30000)
                logger.info(f"Документ разделён на {len(chunks)} блоков")

                # Анализируем документ по частям
                analysis_result = await analyze_document_chunks(
                    chunks=chunks,
                    document_name=file.filename,
                    document_type="",  # модель сама определит тип
                    law_type=legislation,
                    procurement_method=procurement_method
                )

                if analysis_result["status"] == "error":
                    final_doc_type = "Дополнительные материалы"
                    is_valid = False
                    conclusion = f"Ошибка анализа: {analysis_result['analysis']['conclusion']}"
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
                    
                    conclusion = analysis["conclusion"]
                    
                    # Сохраняем результаты анализа для проверки комплектности
                    document_analysis_results[file.filename] = analysis

                    # Сохраняем в БД
                    try:
                        save_raw_data(
                            procurement_id=procurement_id, 
                            document_type=final_doc_type, 
                            full_analysis=analysis
                        )
                        save_clean_conclusion(
                            procurement_id=procurement_id, 
                            document_type=final_doc_type, 
                            conclusion=conclusion
                        )

                        logger.info(f"Сохранено: {procurement_id}, {final_doc_type}")
                    except Exception as db_error:
                        logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                        conclusion += " [Ошибка сохранения в БД]"

                # Формируем результат для документа
                document_result = {
                    "procurement_id": procurement_id,
                    "document_type": final_doc_type,
                    "filename": file.filename,
                    "is_valid": is_valid,
                    "error": None,
                    "conclusion": conclusion
                }
                
                documents_results.append(document_result)

            except Exception as e:
                logger.error(f"Ошибка при обработке {file.filename}: {str(e)}", exc_info=True)
                # Формируем результат с ошибкой
                document_result = {
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": file.filename,
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка обработки: {str(e)}"
                }
                documents_results.append(document_result)

            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        except Exception as e:
            logger.error(f"Критическая ошибка при обработке {file.filename}: {str(e)}", exc_info=True)
            document_result = {
                "procurement_id": procurement_id,
                "document_type": "Дополнительные материалы",
                "filename": file.filename,
                "is_valid": False,
                "error": str(e),
                "conclusion": f"Критическая ошибка: {str(e)}"
            }
            documents_results.append(document_result)

    # Проверяем комплектность документов
    completeness_check = {}
    overall_completeness_conclusion = ""
    
    if document_analysis_results:
        completeness_check = check_procurement_completeness(files_dict, document_analysis_results)
        
        # Формируем общее заключение по комплектности
        if completeness_check["status"] == "allow":
            overall_completeness_conclusion = "Комплект документов полный. Все необходимые документы присутствуют."
        else:
            missing_docs = ", ".join(completeness_check["missing_in_upload"])
            overall_completeness_conclusion = f"Комплект документов неполный. Отсутствуют: {missing_docs}"
        
        # Сохраняем результат проверки комплектности
        save_clean_conclusion(
            procurement_id=procurement_id,
            document_type="completeness_check",
            conclusion=overall_completeness_conclusion
        )

    # Проверяем согласованность данных между документами
    consistency_result = await check_documents_consistency(procurement_id)
    
    # Формируем общее заключение по согласованности
    overall_consistency_conclusion = consistency_result["conclusion"]
    if consistency_result["status"] == "ok":
        overall_consistency_conclusion = "Данные в документах согласованы."
    else:
        overall_consistency_conclusion = f"Обнаружены расхождения в документах: {consistency_result['conclusion']}"
    
    save_clean_conclusion(
        procurement_id=procurement_id,
        document_type="completeness_check",
        conclusion=overall_consistency_conclusion
    )

    # Формируем общее заключение по комплекту документов
    overall_conclusion_text, overall_status = generate_overall_conclusion(
        documents_results, 
        completeness_check, 
        consistency_result
    )

    # Кэшируем результаты для возможного последующего использования
    document_analysis_cache[procurement_id] = {
        "documents": documents_results,
        "completeness_check": completeness_check,
        "consistency_check": consistency_result,
        "overall_conclusion": overall_status
    }

    # Возвращаем новый формат ответа
    return {
        "procurement_id": procurement_id,
        "documents": documents_results,
        "overall_conclusion": overall_status,
        "completeness_summary": {
            "status": completeness_check.get("status", "unknown"),
            "conclusion": overall_completeness_conclusion,
            "missing_documents": completeness_check.get("missing_in_upload", [])
        },
        "consistency_summary": {
            "status": consistency_result.get("status", "unknown"),
            "conclusion": overall_consistency_conclusion,
            "issues": consistency_result.get("issues", [])
        }
    }


def generate_overall_conclusion(
    documents_results: List[Dict], 
    completeness_check: Dict, 
    consistency_result: Dict
) -> str:
    """
    Генерирует итоговое заключение по результатам комплексной проверки комплекта документов.

    Формирует текстовый вывод на основе анализа трёх аспектов: валидности отдельных документов,
    полноты комплекта и согласованности данных между документами. Подсчитывает количество проблем
    и формирует итоговую оценку с рекомендациями.

    Args:
        documents_results (List[Dict]): Список результатов проверки каждого документа, 
            где каждый словарь содержит ключ `is_valid` (bool).
        completeness_check (Dict): Результат проверки полноты комплекта, должен содержать:
            - status (str): 'allow' — комплект полный, иначе — неполный.
            - missing_in_upload (List[str]): Список отсутствующих документов (если есть).
        consistency_result (Dict): Результат проверки согласованности данных между документами, должен содержать:
            - status (str): 'ok' — всё согласовано, иначе — есть расхождения.
            - issues (List[dict]): Список выявленных несоответствий.

    Returns:
        str: Текстовое заключение, содержащее:
            - Количество обработанных и проблемных документов.
            - Статус полноты комплекта.
            - Статус согласованности данных.
            - Итоговую оценку: "соответствует", "требует исправлений" или "требует доработки".
            Все пункты объединены в читаемый отчёт, разделённый переносами строк.
    """
    conclusions = []
    
    # Анализируем результаты по документам
    valid_docs = [doc for doc in documents_results if doc.get("is_valid")]
    invalid_docs = [doc for doc in documents_results if not doc.get("is_valid")]
    
    if valid_docs:
        conclusions.append(f"Обработано документов: {len(valid_docs)}")
    
    if invalid_docs:
        conclusions.append(f"Проблемных документов: {len(invalid_docs)}")
    
    # Добавляем информацию о комплектности
    if completeness_check.get("status") == "allow":
        conclusions.append("Комплект документов полный")
    else:
        missing_count = len(completeness_check.get("missing_in_upload", []))
        conclusions.append(f"Отсутствует документов: {missing_count}")
    
    # Добавляем информацию о согласованности
    if consistency_result.get("status") == "ok":
        conclusions.append("Данные согласованы")
    else:
        issues_count = len(consistency_result.get("issues", []))
        conclusions.append(f"Обнаружено расхождений: {issues_count}")
    
    # Формируем итоговую оценку
    total_issues = len(invalid_docs) + len(completeness_check.get("missing_in_upload", [])) + len(consistency_result.get("issues", []))
    
    # ОПРЕДЕЛЯЕМ ОБЩИЙ СТАТУС
    has_errors = (
        len(invalid_docs) > 0 or 
        completeness_check.get("status") != "allow" or 
        consistency_result.get("status") != "ok"
    )
    
    if not has_errors:
        final_assessment = "Комплект документов соответствует требованиям."
        overall_status = "allow"
    elif total_issues <= 2:
        final_assessment = "Комплект документов в основном соответствует требованиям, но требуются исправления."
        overall_status = "deny"
    else:
        final_assessment = "Комплект документов требует значительной доработки."
        overall_status = "deny"
    
    conclusions.append(f"\nИТОГ: {final_assessment}")
    
    # Сохраняем общий статус для использования в ответе
    return "\n".join(conclusions), overall_status


@app.post("/get-procurement-completeness")
async def api_get_procurement_completeness(request_body: Dict[str, str] = Body(...)):
    """
    Обработчик API-запроса для получения результатов проверки полноты комплекта документов закупки.

    Принимает идентификатор закупки и возвращает сохранённый результат проверки на наличие всех
    обязательных документов. Данные берутся из кэша анализа (document_analysis_cache), который
    должен быть предварительно заполнен при обработке документов.

    Args:
        request_body (Dict[str, str], optional): Тело запроса в формате JSON, содержащее ключ "procurement_id".
            Пример: {"procurement_id": "12345"}.

    Raises:
        HTTPException: Если в теле запроса отсутствует поле `procurement_id` — возвращает ошибку 400.

    Returns:
        dict: Результат проверки полноты комплекта, содержащий:
            - status (str): Статус ("allow" — комплект полный, другой статус — неполный).
            - provided_documents (List[str]): Список найденных типов документов.
            - missing_in_upload (List[str]): Список типов документов, которые должны быть загружены, но отсутствуют.
            - missing_optional (List[str]): Список отсутствующих опциональных документов (если применимо).
            Если данные по указанному `procurement_id` не найдены — возвращается объект с ключом "error".
    """
    procurement_id = request_body.get("procurement_id")
    
    if not procurement_id:
        raise HTTPException(status_code=400, detail="Поле 'procurement_id' обязательно")
    
    if procurement_id not in document_analysis_cache:
        return {"error": "Данные для указанной закупки не найдены"}
    
    return document_analysis_cache[procurement_id]["completeness_check"]


@app.post("/get-documents-report")
async def api_get_documents_report(request_body: Dict[str, str] = Body(...)):
    """
    Обработчик API-запроса для получения сводного отчёта по анализу документов закупки.

    Возвращает структурированный отчёт, включающий список обработанных документов, итоговое заключение,
    а также результаты проверок на полноту и согласованность данных. Данные берутся из кэша анализа.

    Args:
        request_body (Dict[str, str], optional): Тело запроса в формате JSON, содержащее ключ "procurement_id".
            Пример: {"procurement_id": "12345"}.

    Raises:
        HTTPException: Если в теле запроса отсутствует обязательное поле `procurement_id` — 
            возвращает ошибку 400 с соответствующим описанием.

    Returns:
        dict: Словарь с детальным отчётом по закупке, содержащий:
            - procurement_id (str): Идентификатор закупки.
            - documents (List[dict]): Список всех проанализированных документов с их статусом и типом.
            - overall_conclusion (str): Общее текстовое заключение по результатам анализа.
            - completeness_summary (dict): Краткая информация о полноте комплекта:
                * status (str): Статус ("allow" или другой).
                * conclusion (str): Комментарий к проверке полноты.
                * missing_documents (List[str]): Перечень отсутствующих документов.
            - consistency_summary (dict): Краткая информация о согласованности данных:
                * status (str): Статус ("ok" или содержит ошибки).
                * conclusion (str): Итоговый вывод по согласованности.
                * issues (List[dict]): Список выявленных расхождений между документами.
            Если данные по указанному ID не найдены — возвращается объект с ключом "error".
    """
    procurement_id = request_body.get("procurement_id")
    
    if not procurement_id:
        raise HTTPException(status_code=400, detail="Поле 'procurement_id' обязательно")
    
    if procurement_id not in document_analysis_cache:
        return {"error": "Данные для указанной закупки не найдены"}
    
    cache_data = document_analysis_cache[procurement_id]
    
    return {
        "procurement_id": procurement_id,
        "documents": cache_data["documents"],
        "overall_conclusion": cache_data["overall_conclusion"],
        "completeness_summary": {
            "status": cache_data["completeness_check"].get("status", "unknown"),
            "conclusion": cache_data["completeness_check"].get("feedback", ""),
            "missing_documents": cache_data["completeness_check"].get("missing_in_upload", [])
        },
        "consistency_summary": {
            "status": cache_data["consistency_check"].get("status", "unknown"),
            "conclusion": cache_data["consistency_check"].get("conclusion", ""),
            "issues": cache_data["consistency_check"].get("issues", [])
        }
    }


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
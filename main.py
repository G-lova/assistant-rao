import os
import tempfile
import logging
import json

import pandas as pd
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict

from configs.utils import (APIKeyMiddleware,
                           read_file,
                           create_summary_report,
                           generate_overall_conclusion,
                           extract_provided_docs_from_results,
                           extract_required_docs_from_analysis)
from configs.working_with_db import (save_raw_data,
                                     save_clean_conclusion,
                                     get_contract_info_from_db,
                                     save_summary_report)
from src.evaluator import (check_documents_consistency,
                           analyze_document_chunks,
                           split_large_text,
                           check_completeness_with_ai)
from src.scoring import scoring
from configs.parsing import parse_cloud_storage_link, is_cloud_storage_link, process_input_links


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
    files: List[UploadFile] = File(None),
    links: List[str] = Form(None),
    legislation: Optional[str] = Form("44-ФЗ"),
    procurement_method: Optional[str] = Form("Конкурс"),
    expertise_details: Optional[str] = Form("Полный комплект документов о закупке")
):
    """
    Обработчик POST-запроса для массовой проверки документов закупки.

    Принимает файлы и/или ссылки на облачные хранилища, анализирует каждый документ 
    с автоматическим определением его типа, проверяет полноту комплекта и согласованность данных.

    Args:
        procurement_id (str): Уникальный идентификатор закупки для группировки документов.
        files (List[UploadFile], optional): Список загруженных файлов (PDF, DOCX, XLSX и др.).
        links (List[str], optional): Список ссылок на облачные хранилища.
        legislation (Optional[str], optional): Нормативная база. По умолчанию — "44-ФЗ".
        procurement_method (Optional[str], optional): Способ проведения закупки. По умолчанию — "Конкурс".
        expertise_details (Optional[str], optional): Дополнительные сведения об экспертизе.

    Returns:
        dict: Структурированный ответ с результатами анализа.
    """
    global document_analysis_cache
    
    documents_results = []
    document_analysis_results = {}
    files_dict = {}

    # Проверяем, что есть хотя бы файлы или ссылки
    if not files and not links:
        raise HTTPException(
            status_code=400, 
            detail="Необходимо предоставить либо файлы, либо ссылки на документы"
        )

    # ОБРАБОТКА ФАЙЛОВ
    if files:
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
                    chunks = split_large_text(extracted_text, max_chunk_size=20000)
                    logger.info(f"Документ разделён на {len(chunks)} блоков")

                    # Анализируем документ по частям
                    analysis_result = await analyze_document_chunks(
                        chunks=chunks,
                        document_name=file.filename,
                        document_type="",
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
                        final_doc_type = actual_type_from_model if actual_type_from_model else "Дополнительные материалы"

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
                        "conclusion": conclusion,
                        "source": "uploaded_file"
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
                        "conclusion": f"Критическая ошибка обработки: {str(e)}",
                        "source": "uploaded_file"
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
                    "conclusion": f"Критическая ошибка: {str(e)}",
                    "source": "uploaded_file"
                }
                documents_results.append(document_result)

    # ОБРАБОТКА ССЫЛОК НА ОБЛАЧНЫЕ ХРАНИЛИЩА
    if links:
        logger.info(f"Обработка {len(links)} ссылок для закупки {procurement_id}")
        
        for link in links:
            try:
                logger.info(f"Обработка ссылки: {link}")
                
                # Парсим ссылку на облачное хранилище - используем await для асинхронной функции
                parse_result = await parse_cloud_storage_link(link, procurement_id)
                
                if parse_result["status"] == "success":
                    analysis = parse_result.get("analysis", {})
                    filename = parse_result.get("filename", f"cloud_doc_{hash(link)}")
                    
                    # Добавляем анализ в общие результаты
                    document_analysis_results[filename] = analysis
                    
                    # Определяем тип документа и валидность
                    if analysis.get("status") == "success":
                        doc_analysis = analysis.get("analysis", {})
                        actual_type = doc_analysis.get("type_compliance", {}).get("actual_type", "")
                        final_doc_type = actual_type if actual_type else "Дополнительные материалы"
                        confidence = doc_analysis.get("type_compliance", {}).get("confidence", 0)
                        is_valid = (
                            final_doc_type != "Дополнительные материалы" 
                            and confidence >= 0.7
                        )
                        conclusion = doc_analysis.get("conclusion", "Документ обработан из облачного хранилища")
                    else:
                        final_doc_type = "Дополнительные материалы"
                        is_valid = False
                        conclusion = f"Ошибка анализа облачного документа: {analysis.get('error', 'Неизвестная ошибка')}"
                    
                    # Формируем результат для облачного документа
                    document_result = {
                        "procurement_id": procurement_id,
                        "document_type": final_doc_type,
                        "filename": filename,
                        "is_valid": is_valid,
                        "error": None,
                        "conclusion": conclusion,
                        "source": "cloud_storage",
                        "original_link": link
                    }
                    
                    documents_results.append(document_result)
                    
                    # Сохраняем в БД
                    try:
                        if analysis.get("status") == "success":
                            save_raw_data(
                                procurement_id=procurement_id, 
                                document_type=final_doc_type, 
                                full_analysis=doc_analysis
                            )
                            save_clean_conclusion(
                                procurement_id=procurement_id, 
                                document_type=final_doc_type, 
                                conclusion=conclusion
                            )
                            logger.info(f"Сохранен облачный документ: {filename}")
                    except Exception as db_error:
                        logger.error(f"Ошибка сохранения облачного документа в БД: {str(db_error)}")
                
                else:
                    # Обработка ошибок парсинга ссылки
                    logger.error(f"Ошибка парсинга ссылки {link}: {parse_result.get('error')}")
                    document_result = {
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": f"failed_link_{hash(link)}",
                        "is_valid": False,
                        "error": parse_result.get("error", "Неизвестная ошибка парсинга"),
                        "conclusion": f"Ошибка обработки ссылки: {parse_result.get('error', 'Неизвестная ошибка')}",
                        "source": "cloud_storage",
                        "original_link": link
                    }
                    documents_results.append(document_result)
                    
            except Exception as e:
                logger.error(f"Критическая ошибка при обработке ссылки {link}: {str(e)}", exc_info=True)
                document_result = {
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": f"error_link_{hash(link)}",
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка обработки ссылки: {str(e)}",
                    "source": "cloud_storage", 
                    "original_link": link
                }
                documents_results.append(document_result)

    # ПРОВЕРКА КОМПЛЕКТНОСТИ ДОКУМЕНТОВ
    completeness_check = {}
    overall_completeness_conclusion = ""
    
    if document_analysis_results:
        # Используем ИИ для проверки комплектности
        ai_completeness_result = await check_completeness_with_ai(
            procurement_id=procurement_id,
            required_documents=extract_required_docs_from_analysis(document_analysis_results),
            provided_documents=extract_provided_docs_from_results(documents_results)
        )
        
        # Преобразуем результат ИИ в совместимый формат
        completeness_check = {
            "status": "allow" if ai_completeness_result.get("completeness_status") == "полный" else "deny",
            "declared_attachments": extract_required_docs_from_analysis(document_analysis_results),
            "missing_in_upload": ai_completeness_result.get("missing_documents", []),
            "provided_documents": extract_provided_docs_from_results(documents_results),
            "source_docs_for_attached": list(document_analysis_results.keys()),
            "feedback": ai_completeness_result.get("reasoning", ""),
            "detailed_issues": [],
            "final_feedback": f"Статус комплектности: {ai_completeness_result.get('completeness_status', 'неизвестно')}. {ai_completeness_result.get('reasoning', '')}"
        }
        
        # Сохраняем результат проверки комплектности
        save_clean_conclusion(
            procurement_id=procurement_id,
            document_type="completeness_check",
            conclusion=completeness_check["final_feedback"]
        )

    # ПРОВЕРКА СОГЛАСОВАННОСТИ ДАННЫХ
    consistency_result = await check_documents_consistency(procurement_id)
    
    # Формируем общее заключение по согласованности
    overall_consistency_conclusion = consistency_result["conclusion"]
    if consistency_result["status"] == "ok":
        overall_consistency_conclusion = "Данные в документах согласованы."
    else:
        overall_consistency_conclusion = f"Обнаружены расхождения в документах: {consistency_result['conclusion']}"
    
    # Сохраняем результат проверки согласованности
    save_clean_conclusion(
        procurement_id=procurement_id,
        document_type="consistency_check",
        conclusion=overall_consistency_conclusion
    )

    # ФОРМИРУЕМ ОБЩЕЕ ЗАКЛЮЧЕНИЕ
    overall_conclusion_text, overall_status = generate_overall_conclusion(
        documents_results, 
        completeness_check, 
        consistency_result
    )

    # КЭШИРУЕМ РЕЗУЛЬТАТЫ
    document_analysis_cache[procurement_id] = {
        "documents": documents_results,
        "completeness_check": completeness_check,
        "consistency_check": consistency_result,
        "overall_conclusion": overall_status
    }

    # СОЗДАЕМ СВОДНЫЙ ОТЧЕТ
    try:
        summary_report = create_summary_report(
            procurement_id=procurement_id,
            documents_results=documents_results,
            document_analysis_results=document_analysis_results,
            completeness_check=completeness_check,
            consistency_result=consistency_result
        )

        # Сохраняем сводный отчет
        save_summary_report(procurement_id, summary_report)

        logger.info(f"Сводный отчет создан и сохранен для procurement_id={procurement_id}")
    except Exception as e:
        logger.error(f"Ошибка при создании сводного отчета: {str(e)}")

    # ВОЗВРАЩАЕМ РЕЗУЛЬТАТ
    return {
        "procurement_id": procurement_id,
        "documents_processed": len(documents_results),
        "files_processed": len([d for d in documents_results if d.get("source") == "uploaded_file"]),
        "links_processed": len([d for d in documents_results if d.get("source") == "cloud_storage"]),
        "documents": documents_results,
        "overall_conclusion": overall_status,
        "completeness_summary": {
            "status": completeness_check.get("status", "unknown"),
            "conclusion": completeness_check.get("final_feedback", ""),
            "missing_documents": completeness_check.get("missing_in_upload", [])
        },
        "consistency_summary": {
            "status": consistency_result.get("status", "unknown"),
            "conclusion": overall_consistency_conclusion,
            "issues": consistency_result.get("issues", [])
        }
    }


@app.post("/process-links")
async def process_links(
    procurement_id: str = Form(...),
    links: List[str] = Form(...),
    legislation: Optional[str] = Form("44-ФЗ"),
    procurement_method: Optional[str] = Form("Конкурс")
):
    """
    Обработчик для парсинга данных из облачных хранилищ и прямых ссылок
    """
    try:
        results = await process_input_links(links, procurement_id)
        
        return {
            "procurement_id": procurement_id,
            "processed_links": len(results),
            "successful": len([r for r in results if r["result"]["status"] == "success"]),
            "failed": len([r for r in results if r["result"]["status"] == "error"]),
            "results": results
        }
        
    except Exception as e:
        logger.error(f"Ошибка обработки ссылок: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки ссылок: {str(e)}")


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
            "conclusion": cache_data["completeness_check"].get("final_feedback", ""),
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


@app.post("/get-experts-for-expertise")
async def get_experts_for_expertise(request_body: Dict[str, int] = Body(...)):
    """
    Обработчик API-запроса для подбора подходящих экспертов по идентификатору экспертизы.
    Возвращает список ID экспертов.

    Args:
        request_body (Dict[str, int], optional): Тело запроса в формате JSON, содержащее ключ "expertise_id".
            Пример: {"expertise_id": 12345}.

    Raises:
        HTTPException: Если поле `expertise_id` отсутствует — возвращает ошибку 400.
        HTTPException: При возникновении внутренней ошибки в процессе обработки — возвращает ошибку 500.

    Returns:
        List[int]: Список идентификаторов экспертов, отсортированных по рейтингу.
    """
    try:
        expertise_id = request_body.get("expertise_id")
        
        if not expertise_id:
            raise HTTPException(status_code=400, detail="Поле 'expertise_id' обязательно")
        
        # Запускаем скоринг пайплайн
        results = scoring(expertise_id)
        
        # Преобразуем результат в список целых чисел
        if hasattr(results, 'tolist'):
            expert_ids = results.tolist()  # Для Series или массива
        elif isinstance(results, list):
            expert_ids = results
        else:
            # Предполагаем, что это DataFrame — извлекаем первую колонку или 'expert_id'
            col = 'expert_id' if 'expert_id' in results.columns else results.columns[0]
            expert_ids = results[col].tolist()
        
        # Убедимся, что все элементы — int
        expert_ids = [int(x) for x in expert_ids]
        
        return expert_ids  # FastAPI автоматически сериализует в JSON
        
    except Exception as e:
        logger.error(f"Ошибка при подборе экспертов: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка при подборе экспертов: {str(e)}")


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
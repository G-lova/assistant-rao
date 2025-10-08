import os
import tempfile
import logging
import asyncio

import pandas as pd
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict

from configs.utils import (APIKeyMiddleware,
                           read_file,
                           create_summary_report,
                           generate_overall_conclusion,
                           extract_provided_docs_from_results,
                           extract_required_docs_from_analysis,
                           evaluate_documents_batch_internal,
                           save_document_data)
from configs.working_with_db import (save_raw_data,
                                     save_clean_conclusion,
                                     get_contract_info_from_db,
                                     save_summary_report)
from src.evaluator import (check_documents_consistency,
                           analyze_document_chunks,
                           split_large_text,
                           check_completeness_with_ai)
from src.scoring import scoring
from configs.parsing import parse_cloud_storage_link, execute_with_retry
from configs.retry_utils import async_retry, API_RETRY_CONFIG, EXPERTS_RETRY_CONFIG
from celery_app import celery_app


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


@app.post("/internal/parse-cloud-link")
async def internal_parse_cloud_link(
    url: str = Form(...),
    procurement_id: str = Form(None)
):
    """
    Внутренний эндпоинт для парсинга документа по ссылке из облачного хранилища.

    Принимает URL на документ (например, из Яндекс.Диска или Google Drive) и опциональный
    идентификатор закупки, извлекает содержимое документа, анализирует его и возвращает
    структурированный результат. Используется для интеграции с внешними системами
    или фоновой обработки ссылок.

    Args:
        url (str, optional): URL на документ в облачном хранилище. Обязательный параметр,
            передаётся в теле запроса как form-data.
        procurement_id (str, optional): Уникальный идентификатор закупки для логирования
            и сохранения результатов. Может отсутствовать.

    Returns:
        _type_: JSON-ответ с полем "status" ("success" или "error"), исходным URL
            и либо результатом парсинга ("result"), либо сообщением об ошибке ("error").
    """
    try:
        logger.info(f"Внутренний парсинг ссылки: {url} для закупки {procurement_id}")
        
        # Парсим ссылку напрямую
        parse_result = await parse_cloud_storage_link(url, procurement_id)
        
        return {
            "status": "success",
            "url": url,
            "result": parse_result
        }
        
    except Exception as e:
        logger.error(f"Ошибка внутреннего парсинга ссылки: {str(e)}")
        return {
            "status": "error",
            "url": url,
            "error": str(e)
        }
    

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
    Принимает пакет документов (файлы и/или ссылки) и запускает их комплексную экспертизу.

    Эндпоинт обрабатывает загруженные файлы и ссылки на документы из облачных хранилищ,
    извлекает текст, определяет типы документов с помощью ИИ, проверяет комплектность
    и согласованность данных, сохраняет результаты в БД и возвращает структурированный
    отчёт. Поддерживает повторные попытки при временных ошибках и корректно обрабатывает
    исключительные ситуации на всех этапах.

    Args:
        procurement_id (str, optional): Уникальный идентификатор закупки. Обязательный параметр.
        files (List[UploadFile], optional): Список загружаемых файлов документов.
        links (List[str], optional): Список URL на документы в облачных хранилищах.
        legislation (Optional[str], optional): Применимое законодательство (по умолчанию "44-ФЗ").
        procurement_method (Optional[str], optional): Способ закупки (по умолчанию "Конкурс").
        expertise_details (Optional[str], optional): Описание комплекта документов (не используется напрямую).

    Raises:
        HTTPException: Если не переданы ни файлы, ни ссылки.
        ValueError: При обнаружении пустого файла или невозможности извлечь текст.

    Returns:
        _type_: JSON-ответ с полной информацией об обработке: список документов с типами
            и заключениями, результаты проверок комплектности и согласованности,
            общее заключение и метаданные обработки.
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

    # === ОБРАБОТКА ФАЙЛОВ ===
    if files:
        for file in files:
            try:
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
                    extracted_text = read_file(tmp_path, original_filename=file.filename)
                    if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                        raise ValueError("Не удалось извлечь текст из файла")

                    chunks = split_large_text(extracted_text, max_chunk_size=20000)
                    logger.info(f"Документ разделён на {len(chunks)} блоков")

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
                        final_doc_type = actual_type_from_model if actual_type_from_model else "Дополнительные материалы"
                        confidence = analysis["type_compliance"].get("confidence", 0)
                        is_valid = (
                            final_doc_type != "Дополнительные материалы"
                            and confidence >= 0.7
                        )
                        conclusion = analysis["conclusion"]
                        document_analysis_results[file.filename] = analysis

                        try:
                            save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                            save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=conclusion)
                            logger.info(f"Сохранено: {procurement_id}, {final_doc_type}")
                        except Exception as db_error:
                            logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                            conclusion += " [Ошибка сохранения в БД]"

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

    # ОБРАБОТКА ССЫЛОК с ретраями
    if links:
        logger.info(f"Обработка {len(links)} ссылок с ретраями для закупки {procurement_id}")
        
        for link in links:
            try:
                logger.info(f"Обработка ссылки с ретраями: {link}")
                
                # Парсим ссылку с ретраями
                parse_result = await execute_with_retry(
                    parse_cloud_storage_link,
                    link,
                    procurement_id,
                    max_retries=1,
                    base_delay=5
                )
                
                if parse_result.get("status") == "success":
                    filename = parse_result.get("filename", f"cloud_doc_{hash(link)}")
                    file_path = parse_result.get("file_path")  # Путь к скачанному файлу
                    
                    if file_path and os.path.exists(file_path):
                        try:
                            # ОБРАБАТЫВАЕМ СКАЧАННЫЙ ФАЙЛ как обычный файл
                            extracted_text = read_file(file_path, original_filename=filename)

                            if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                                raise ValueError("Не удалось извлечь текст из файла")

                            # Разделяем большой текст на блоки
                            chunks = split_large_text(extracted_text, max_chunk_size=20000)

                            # Анализируем документ по частям с ретраями
                            analysis_result = await execute_with_retry(
                                analyze_document_chunks,
                                chunks,
                                filename,
                                "",
                                legislation,
                                procurement_method,
                                max_retries=2,
                                base_delay=1
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
                                document_analysis_results[filename] = analysis

                                # Сохраняем в БД с ретраями
                                try:
                                    await execute_with_retry(
                                        save_document_data,
                                        procurement_id,
                                        final_doc_type,
                                        analysis,
                                        conclusion,
                                        max_retries=1,
                                        base_delay=1
                                    )
                                    logger.info(f"Сохранен облачный документ: {filename}")
                                except Exception as db_error:
                                    logger.error(f"Ошибка сохранения облачного документа в БД: {str(db_error)}")
                                    conclusion += " [Ошибка сохранения в БД]"

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
                            
                        except Exception as processing_error:
                            logger.error(f"Ошибка обработки скачанного файла {filename}: {str(processing_error)}")
                            document_result = {
                                "procurement_id": procurement_id,
                                "document_type": "Дополнительные материалы",
                                "filename": filename,
                                "is_valid": False,
                                "error": str(processing_error),
                                "conclusion": f"Ошибка обработки скачанного файла: {str(processing_error)}",
                                "source": "cloud_storage",
                                "original_link": link
                            }
                        
                        finally:
                            # ВСЕГДА удаляем временный файл после обработки
                            try:
                                if os.path.exists(file_path):
                                    os.unlink(file_path)
                                    logger.info(f"Временный файл удален: {file_path}")
                            except Exception as cleanup_error:
                                logger.warning(f"Не удалось удалить временный файл {file_path}: {cleanup_error}")
                    
                    else:
                        # Файл не был скачан или не существует
                        error_msg = "Файл не был скачан или временный файл отсутствует"
                        logger.error(f"Ошибка обработки ссылки {link}: {error_msg}")
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": error_msg,
                            "conclusion": f"Ошибка обработки ссылки: {error_msg}",
                            "source": "cloud_storage",
                            "original_link": link
                        }
                    
                    documents_results.append(document_result)
                
                else:
                    # Ошибка парсинга после всех ретраев
                    error_msg = parse_result.get("error", "Неизвестная ошибка парсинга")
                    logger.error(f"Ошибка парсинга ссылки {link} после ретраев: {error_msg}")
                    document_result = {
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": f"failed_link_{hash(link)}",
                        "is_valid": False,
                        "error": error_msg,
                        "conclusion": f"Ошибка обработки ссылки: {error_msg}",
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

    # === ПРОВЕРКА КОМПЛЕКТНОСТИ ===
    completeness_check = {}
    if document_analysis_results:
        ai_completeness_result = await check_completeness_with_ai(
            procurement_id=procurement_id,
            required_documents=extract_required_docs_from_analysis(document_analysis_results),
            provided_documents=extract_provided_docs_from_results(documents_results)
        )
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
        save_clean_conclusion(procurement_id=procurement_id, document_type="completeness_check", conclusion=completeness_check["final_feedback"])

    # === ПРОВЕРКА СОГЛАСОВАННОСТИ ===
    consistency_result = await check_documents_consistency(procurement_id)
    overall_consistency_conclusion = (
        "Данные в документах согласованы."
        if consistency_result["status"] == "ok"
        else f"Обнаружены расхождения в документах: {consistency_result['conclusion']}"
    )
    save_clean_conclusion(procurement_id=procurement_id, document_type="consistency_check", conclusion=overall_consistency_conclusion)

    # === ОБЩЕЕ ЗАКЛЮЧЕНИЕ ===
    overall_conclusion_text, overall_status = generate_overall_conclusion(
        documents_results, 
        completeness_check, 
        consistency_result
    )

    # === КЭШИРОВАНИЕ ===
    document_analysis_cache[procurement_id] = {
        "documents": documents_results,
        "completeness_check": completeness_check,
        "consistency_check": consistency_result,
        "overall_conclusion": overall_status
    }

    # === СВОДНЫЙ ОТЧЁТ ===
    try:
        summary_report = create_summary_report(
            procurement_id=procurement_id,
            documents_results=documents_results,
            document_analysis_results=document_analysis_results,
            completeness_check=completeness_check,
            consistency_result=consistency_result
        )
        save_summary_report(procurement_id, summary_report)
        logger.info(f"Сводный отчет создан и сохранен для procurement_id={procurement_id}")
    except Exception as e:
        logger.error(f"Ошибка при создании сводного отчета: {str(e)}")

    # === ОТВЕТ ===
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
@async_retry(EXPERTS_RETRY_CONFIG)
async def get_experts_for_expertise(request_body: Dict[str, int] = Body(...)):
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
        results = scoring(expertise_id)
        
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


# Добавляем Celery задачи с повторными попытками
@celery_app.task(bind=True, max_retries=1)
def parse_cloud_link_task(self, url: str, procurement_id: str = None):
    """
    Celery-задача для асинхронного парсинга документа по ссылке из облачного хранилища.

    Выполняет извлечение и анализ документа по указанной URL-ссылке в фоновом режиме.
    При возникновении ошибки автоматически повторяет попытку один раз с задержкой 3 секунды.

    Args:
        url (str): URL на документ в облачном хранилище (например, Яндекс.Диск, Google Drive).
        procurement_id (str, optional): Идентификатор закупки для логирования и привязки результата.
            По умолчанию None.

    Raises:
        self.retry: В случае исключения задача будет автоматически повторена
            (максимум 1 раз, как указано в декораторе).

    Returns:
        _type_: Результат парсинга — словарь с полями "status", "filename", "file_path", "analysis" и др.,
            возвращаемый функцией parse_cloud_storage_link.
    """
    try:
        # Используем синхронную версию или запускаем асинхронную в event loop
        result = asyncio.run(parse_cloud_storage_link(url, procurement_id))
        return result
    except Exception as exc:
        # Автоматический retry от Celery
        raise self.retry(countdown=3, exc=exc)


@celery_app.task(bind=True, max_retries=1)
def evaluate_documents_task(self, task_data: dict):
    """
    Celery-задача для фоновой обработки пакета документов закупки.

    Запускает полный цикл анализа документов (файлов и/или ссылок) в асинхронном режиме:
    извлечение текста, определение типов, проверка комплектности и согласованности,
    сохранение результатов в БД и формирование сводного отчёта. Используется для
    разгрузки HTTP-эндпоинта и обработки длительных операций.

    Args:
        task_data (dict): Словарь с данными задачи, включающий procurement_id, files_data,
            links, legislation, procurement_method и другие параметры, необходимые
            для анализа (см. evaluate_documents_batch_internal).

    Raises:
        self.retry: При возникновении исключения задача повторяется один раз
            с задержкой 2 секунды (согласно настройкам декоратора).

    Returns:
        _type_: Полный результат обработки — словарь с деталями по каждому документу,
            результатами проверок и общим заключением, возвращаемый
            evaluate_documents_batch_internal.
    """
    try:
        result = asyncio.run(evaluate_documents_batch_internal(task_data))
        return result
    except Exception as exc:
        raise self.retry(countdown=2, exc=exc)


@celery_app.task(bind=True, max_retries=1)
def get_experts_task(self, expertise_id: int):
    """
    Celery-задача для фонового подбора экспертов по идентификатору экспертизы.

    Выполняет скоринговую модель для определения наиболее подходящих экспертов
    на основе данных об экспертизе. Результат возвращается в виде списка ID экспертов.
    При ошибке автоматически повторяет выполнение один раз с задержкой 2 секунды.

    Args:
        expertise_id (int): Числовой идентификатор экспертизы, для которой требуется подбор экспертов.

    Raises:
        self.retry: В случае исключения задача будет повторена (максимум 1 раз,
            как указано в декораторе) с задержкой 2 секунды.

    Returns:
        _type_: Результат скоринга — список идентификаторов экспертов (в формате,
            возвращаемом функцией scoring).
    """
    try:
        # Здесь логика получения экспертов
        results = scoring(expertise_id)
        return results
    except Exception as exc:
        raise self.retry(countdown=2, exc=exc)


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
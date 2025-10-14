import os
import tempfile
import logging

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
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, CLOUD_PARSING_RETRY_CONFIG


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

    # ОБРАБОТКА ССЫЛОК
    if links:
        logger.info(f"Обработка {len(links)} ссылок для закупки {procurement_id}")
        for link in links:
            try:
                logger.info(f"Обработка ссылки: {link}")

                # === ПАРСИНГ С РЕТРАЯМИ ===
                parse_result = await async_retry(CLOUD_PARSING_RETRY_CONFIG)(
                    parse_cloud_storage_link
                )(link, procurement_id)

                if parse_result.get("status") != "success":
                    error_msg = parse_result.get("error", "Неизвестная ошибка парсинга")
                    logger.error(f"Ошибка парсинга ссылки {link} после всех ретраев: {error_msg}")
                    document_result = {
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": f"failed_link_{hash(link)}",
                        "is_valid": False,
                        "error": error_msg,
                        "conclusion": f"Не удалось обработать ссылку после повторных попыток: {error_msg}",
                        "source": "external_link",
                        "original_link": link
                    }
                    documents_results.append(document_result)
                    continue

                # === ОБРАБОТКА ОДНОГО ИЛИ НЕСКОЛЬКИХ ФАЙЛОВ ===
                files_to_process = parse_result.get("files", [parse_result])  # ЕИС возвращает список, облака — один файл

                for file_info in files_to_process:
                    file_path = file_info.get("file_path")
                    filename = file_info.get("filename", f"doc_from_{link}")
                    source = file_info.get("source", "external_link")

                    if not file_path or not os.path.exists(file_path):
                        logger.warning(f"Файл не найден после парсинга ссылки {link}: {file_path}")
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": "Файл не был скачан",
                            "conclusion": "Файл не был скачан или повреждён",
                            "source": source,
                            "original_link": link
                        }
                        documents_results.append(document_result)
                        continue

                    try:
                        # === ИЗВЛЕЧЕНИЕ ТЕКСТА ===
                        extracted_text = read_file(file_path, original_filename=filename)
                        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                            raise ValueError("Не удалось извлечь текст из файла")

                        # === РАЗБИВКА НА ЧАНКИ ===
                        chunks = split_large_text(extracted_text, max_chunk_size=20000)

                        # === АНАЛИЗ ДОКУМЕНТА ===
                        analysis_result = await analyze_document_chunks(
                            chunks=chunks,
                            document_name=filename,
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
                            document_analysis_results[filename] = analysis

                            # === СОХРАНЕНИЕ В БД ===
                            try:
                                save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                                save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=conclusion)
                                logger.info(f"Сохранён документ из ссылки: {filename}")
                            except Exception as db_error:
                                logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                                conclusion += " [Ошибка сохранения в БД]"

                        # === ФОРМИРОВАНИЕ РЕЗУЛЬТАТА ===
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": final_doc_type,
                            "filename": filename,
                            "is_valid": is_valid,
                            "error": None,
                            "conclusion": conclusion,
                            "source": source,
                            "original_link": link
                        }
                        documents_results.append(document_result)

                    except Exception as processing_error:
                        logger.error(f"Ошибка обработки файла из ссылки {link}: {processing_error}", exc_info=True)
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": str(processing_error),
                            "conclusion": f"Ошибка обработки файла: {str(processing_error)}",
                            "source": source,
                            "original_link": link
                        }
                        documents_results.append(document_result)
                    finally:
                        # === УДАЛЕНИЕ ВРЕМЕННОГО ФАЙЛА ===
                        try:
                            if os.path.exists(file_path):
                                os.unlink(file_path)
                                logger.info(f"Временный файл удалён: {file_path}")
                        except Exception as cleanup_error:
                            logger.warning(f"Не удалось удалить временный файл {file_path}: {cleanup_error}")

            except Exception as e:
                logger.error(f"Критическая ошибка при обработке ссылки {link}: {e}", exc_info=True)
                document_result = {
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": f"error_link_{hash(link)}",
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка: {str(e)}",
                    "source": "external_link",
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
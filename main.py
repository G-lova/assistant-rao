import os
import tempfile
import logging
import json

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body
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
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


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
    Все документы сохраняются в БД. Затем вызывается ИИ-модель, которая получает **все сырые данные из БД**
    и возвращает структурированный JSON-ответ. Ответ возвращается клиенту **без каких-либо преобразований**.
    """
    global document_analysis_cache

    documents_results = []
    document_analysis_results = {}

    if not files and not links:
        raise HTTPException(
            status_code=400, 
            detail="Необходимо предоставить либо файлы, либо ссылки на документы"
        )

    # === ОБРАБОТКА ФАЙЛОВ (последовательно) ===
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
                        analysis = analysis_result["analysis"]
                    else:
                        analysis = analysis_result["analysis"]
                        actual_type = analysis["type_compliance"].get("actual_type", "").strip()
                        final_doc_type = actual_type if actual_type else "Дополнительные материалы"
                        confidence = analysis["type_compliance"].get("confidence", 0)
                        is_valid = final_doc_type != "Дополнительные материалы" and confidence >= 0.7
                        conclusion = analysis["conclusion"]
                        document_analysis_results[file.filename] = analysis

                        try:
                            save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                            save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=conclusion)
                            logger.info(f"Сохранено: {procurement_id}, {final_doc_type}")
                        except Exception as db_error:
                            logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                            conclusion += " [Ошибка сохранения в БД]"

                    documents_results.append({
                        "procurement_id": procurement_id,
                        "document_type": final_doc_type,
                        "filename": file.filename,
                        "is_valid": is_valid,
                        "error": None,
                        "conclusion": conclusion,
                        "source": "uploaded_file"
                    })

                except Exception as e:
                    logger.error(f"Ошибка при обработке {file.filename}: {str(e)}", exc_info=True)
                    documents_results.append({
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": file.filename,
                        "is_valid": False,
                        "error": str(e),
                        "conclusion": f"Критическая ошибка обработки: {str(e)}",
                        "source": "uploaded_file"
                    })
                finally:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)

            except Exception as e:
                logger.error(f"Критическая ошибка при обработке {file.filename}: {str(e)}", exc_info=True)
                documents_results.append({
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": file.filename,
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка: {str(e)}",
                    "source": "uploaded_file"
                })

    # === ОБРАБОТКА ССЫЛОК (последовательно) ===
    if links:
        logger.info(f"Обработка {len(links)} ссылок для закупки {procurement_id}")
        for link in links:
            try:
                logger.info(f"Обработка ссылки: {link}")
                parse_result = await async_retry(CLOUD_PARSING_RETRY_CONFIG)(
                    parse_cloud_storage_link
                )(link, procurement_id)

                if parse_result.get("status") != "success":
                    error_msg = parse_result.get("error", "Неизвестная ошибка парсинга")
                    logger.error(f"Ошибка парсинга ссылки {link}: {error_msg}")
                    filename = f"failed_link_{abs(hash(link)) % (10**8)}"
                    documents_results.append({
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": filename,
                        "is_valid": False,
                        "error": error_msg,
                        "conclusion": f"Не удалось обработать ссылку: {error_msg}",
                        "source": "external_link",
                        "original_link": link
                    })
                    document_analysis_results[filename] = create_unprocessed_document_analysis(filename, error_msg)
                    continue

                files_to_process = parse_result.get("files", [parse_result])
                for file_info in files_to_process:
                    file_path = file_info.get("file_path")
                    filename = file_info.get("filename", f"doc_from_link_{abs(hash(link)) % (10**8)}")
                    source = file_info.get("source", "external_link")

                    if not file_path or not os.path.exists(file_path):
                        logger.warning(f"Файл не найден: {file_path}")
                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": "Файл не скачан",
                            "conclusion": "Файл отсутствует после парсинга",
                            "source": source,
                            "original_link": link
                        })
                        continue

                    try:
                        extracted_text = read_file(file_path, original_filename=filename)
                        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                            raise ValueError("Не удалось извлечь текст")

                        chunks = split_large_text(extracted_text, max_chunk_size=20000)
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
                            analysis = analysis_result["analysis"]
                        else:
                            analysis = analysis_result["analysis"]
                            actual_type = analysis["type_compliance"].get("actual_type", "").strip()
                            final_doc_type = actual_type if actual_type else "Дополнительные материалы"
                            confidence = analysis["type_compliance"].get("confidence", 0)
                            is_valid = final_doc_type != "Дополнительные материалы" and confidence >= 0.7
                            conclusion = analysis["conclusion"]
                            document_analysis_results[filename] = analysis

                            try:
                                save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                                save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=conclusion)
                                logger.info(f"Сохранён документ из ссылки: {filename}")
                            except Exception as db_error:
                                logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                                conclusion += " [Ошибка сохранения в БД]"

                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": final_doc_type,
                            "filename": filename,
                            "is_valid": is_valid,
                            "error": None,
                            "conclusion": conclusion,
                            "source": source,
                            "original_link": link
                        })

                    except Exception as processing_error:
                        logger.error(f"Ошибка обработки файла из ссылки {link}: {processing_error}", exc_info=True)
                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": str(processing_error),
                            "conclusion": f"Ошибка обработки: {str(processing_error)}",
                            "source": source,
                            "original_link": link
                        })
                    finally:
                        try:
                            if os.path.exists(file_path):
                                os.unlink(file_path)
                                logger.info(f"Временный файл удалён: {file_path}")
                        except Exception as cleanup_error:
                            logger.warning(f"Не удалось удалить файл {file_path}: {cleanup_error}")

            except Exception as e:
                logger.error(f"Критическая ошибка при обработке ссылки {link}: {e}", exc_info=True)
                filename = f"error_link_{abs(hash(link)) % (10**8)}"
                documents_results.append({
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": filename,
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка: {str(e)}",
                    "source": "external_link",
                    "original_link": link
                })

    # === ПРОВЕРКА НАЛИЧИЯ ДАННЫХ ===
    if not document_analysis_results:
        raise HTTPException(status_code=400, detail="Нет данных для анализа: не удалось обработать ни один документ")

    # === ФИНАЛЬНЫЙ ВЫЗОВ ИИ ===
    try:
        final_evaluation_result = await async_retry(API_RETRY_CONFIG)(
            check_completeness_with_ai
        )(procurement_id)
    except Exception as e:
        logger.error(f"Критическая ошибка вызова модели: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось выполнить итоговую проверку")

    # === СОХРАНЕНИЕ ИТОГОВ ===
    try:
        save_clean_conclusion(
            procurement_id=procurement_id,
            document_type="completeness_check",
            conclusion=json.dumps(final_evaluation_result, ensure_ascii=False, indent=2)
        )
    except Exception as e:
        logger.warning(f"Не удалось сохранить итоговое заключение: {e}")

    try:
        summary_report = create_summary_report(
            procurement_id=procurement_id,
            documents_results=documents_results,
            document_analysis_results=document_analysis_results,
            completeness_check=final_evaluation_result,
            consistency_result={"status": "ok", "issues": [], "conclusion": "Проверка согласованности отключена"}
        )
        save_summary_report(procurement_id, summary_report)
        logger.info(f"Сводный отчёт сохранён для {procurement_id}")
    except Exception as e:
        logger.error(f"Ошибка создания сводного отчёта: {e}")

    return final_evaluation_result


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
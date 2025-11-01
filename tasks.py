import os
import tempfile
import asyncio
import logging

from celery import current_task
from celery_app import celery_app

from configs.utils import read_file, create_summary_report
from configs.working_with_db import save_raw_data, save_clean_conclusion, save_summary_report
from src.evaluator import analyze_document_chunks, split_large_text, check_completeness_with_ai, create_unprocessed_document_analysis
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


logger = logging.getLogger(__name__)

def run_async(coro):
    """Вспомогательная функция для запуска async-функций из sync-контекста Celery"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(bind=True, name="evaluate_documents_task")
def evaluate_documents_task(
    self,
    procurement_id: str,
    expertise_customer: str = None,
    file_paths: list = None,
    filenames: list = None,
    links: list = None,
    eis_links: str = None,
    legislation: str = "44-ФЗ",
    procurement_method: str = "Конкурс",
    expertise_details: str = "Полный комплект документов о закупке"
):
    """
    Celery-задача для асинхронной обработки пакета документов.
    Принимает уже сохранённые временные пути файлов или ссылки.
    """
    documents_results = []
    document_analysis_results = {}
    all_links = (links or []) + ([eis_links] if eis_links else [])

    # === Обработка файлов ===
    if file_paths and filenames:
        for file_path, filename in zip(file_paths, filenames):
            try:
                extracted_text = read_file(file_path, original_filename=filename)
                if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                    raise ValueError("Не удалось извлечь текст")

                chunks = split_large_text(extracted_text, max_chunk_size=20000)
                analysis_result = run_async(analyze_document_chunks(
                    chunks=chunks,
                    document_name=filename,
                    document_type="",
                    law_type=legislation,
                    procurement_method=procurement_method
                ))

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

                    save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                    save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=conclusion)

                documents_results.append({
                    "procurement_id": procurement_id,
                    "document_type": final_doc_type,
                    "filename": filename,
                    "is_valid": is_valid,
                    "error": None,
                    "conclusion": conclusion,
                    "source": "uploaded_file"
                })

            except Exception as e:
                logger.error(f"Ошибка при обработке {filename}: {e}")
                documents_results.append({
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": filename,
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка обработки: {str(e)}",
                    "source": "uploaded_file"
                })
            finally:
                if os.path.exists(file_path):
                    os.unlink(file_path)

    # === Обработка ссылок ===
    if all_links:
        for link in all_links:
            if not link:
                continue
            try:
                parse_result = run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(
                    parse_cloud_storage_link
                )(link, procurement_id))

                if parse_result.get("status") != "success":
                    error_msg = parse_result.get("error", "Неизвестная ошибка парсинга")
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

                for file_info in parse_result.get("files", [parse_result]):
                    file_path = file_info.get("file_path")
                    filename = file_info.get("filename", f"doc_from_link_{abs(hash(link)) % (10**8)}")
                    if not file_path or not os.path.exists(file_path):
                        continue

                    try:
                        extracted_text = read_file(file_path, original_filename=filename)
                        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                            raise ValueError("Не удалось извлечь текст")

                        chunks = split_large_text(extracted_text, max_chunk_size=20000)
                        analysis_result = run_async(analyze_document_chunks(
                            chunks=chunks,
                            document_name=filename,
                            document_type="",
                            law_type=legislation,
                            procurement_method=procurement_method
                        ))

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

                            save_raw_data(procurement_id=procurement_id, document_type=final_doc_type, full_analysis=analysis)
                            save_clean_conclusion(procurement_id=procurement_id, document_type=final_doc_type, conclusion=conclusion)

                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": final_doc_type,
                            "filename": filename,
                            "is_valid": is_valid,
                            "error": None,
                            "conclusion": conclusion,
                            "source": file_info.get("source", "external_link"),
                            "original_link": link
                        })

                    except Exception as e:
                        logger.error(f"Ошибка обработки файла из ссылки {link}: {e}")
                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": str(e),
                            "conclusion": f"Ошибка обработки: {str(e)}",
                            "source": file_info.get("source", "external_link"),
                            "original_link": link
                        })
                    finally:
                        if os.path.exists(file_path):
                            os.unlink(file_path)

            except Exception as e:
                logger.error(f"Критическая ошибка при обработке ссылки {link}: {e}")
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

    # === Финальная оценка ===
    if not document_analysis_results:
        raise ValueError("Нет данных для анализа")

    final_evaluation_result = run_async(check_completeness_with_ai(
        procurement_id, expertise_customer=expertise_customer, eis_links=eis_links
    ))

    save_clean_conclusion(
        procurement_id=procurement_id,
        document_type="completeness_check",
        conclusion=str(final_evaluation_result)
    )

    summary_report = create_summary_report(
        procurement_id=procurement_id,
        documents_results=documents_results,
        document_analysis_results=document_analysis_results,
        completeness_check=final_evaluation_result,
        consistency_result={"status": "ok", "issues": [], "conclusion": "Проверка согласованности отключена"}
    )
    save_summary_report(procurement_id, summary_report)

    return final_evaluation_result
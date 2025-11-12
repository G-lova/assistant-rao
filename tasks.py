import os
import tempfile
import asyncio
import logging
import datetime

from celery import current_task
from celery_app import celery_app

from configs.config import Config
from configs.utils import read_file, create_summary_report
from configs.working_with_db import save_raw_data, save_clean_conclusion, save_summary_report, delete_procurement_data
from src.evaluator import analyze_document_chunks, split_large_text, check_completeness_with_ai, create_unprocessed_document_analysis
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


logger = logging.getLogger(__name__)


def run_async(coro):
    """_summary_

    Args:
        coro (_type_): _description_

    Returns:
        _type_: _description_
    """
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
    document_codes: list = None,
    document_labels: list = None,
    comments: list = None,
    links: list = None,
    eis_links: str = None,
    link_to_metadata: dict = None,
    legislation: str = "44-ФЗ",
    procurement_method: str = "Конкурс",
    expertise_details: str = "Полный комплект документов о закупке"
):
    # === 1. Очистка старых данных ===
    try:
        delete_procurement_data(procurement_id)
        logger.info(f"Запись в БД удалена {procurement_id}")
    except:
        logger.info(f"Запись в БД не существует {procurement_id}")

    documents_results = []
    document_analysis_results = {}
    all_links = (links or []) + ([eis_links] if eis_links else [])

    # === Обработка загруженных файлов ===
    if file_paths and filenames:
        for i, (file_path, filename) in enumerate(zip(file_paths, filenames)):
            doc_code = document_codes[i] if document_codes and i < len(document_codes) else "unknown"
            doc_label = document_labels[i] if document_labels and i < len(document_labels) else "Неизвестный документ"
            comment = comments[i] if comments and i < len(comments) else None
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

                    full_analysis_with_meta = {
                        **analysis,
                        "document_code": doc_code,
                        "document_label": doc_label,
                        "comment": comment
                    }
                    save_raw_data(
                        procurement_id=procurement_id,
                        document_type=final_doc_type,
                        full_analysis=full_analysis_with_meta
                    )
                    save_clean_conclusion(
                        procurement_id=procurement_id,
                        document_type=final_doc_type,
                        conclusion=conclusion
                    )

                documents_results.append({
                    "procurement_id": procurement_id,
                    "document_type": final_doc_type,
                    "filename": filename,
                    "is_valid": is_valid,
                    "error": None,
                    "conclusion": conclusion,
                    "source": "uploaded_file",
                    "document_code": doc_code,
                    "document_label": doc_label,
                    "comment": comment
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
                    "source": "uploaded_file",
                    "document_code": doc_code,
                    "document_label": doc_label,
                    "comment": comment
                })
                document_analysis_results[filename] = create_unprocessed_document_analysis(filename, str(e))
            finally:
                if os.path.exists(file_path):
                    os.unlink(file_path)

    # === Обработка ссылок ===
    if all_links:
        for link in all_links:
            if not link:
                continue

            # === ЕИС: только проверка доступности + номер закупки → eis_data ===
            if "zakupki.gov.ru" in link:
                try:
                    parse_result = run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(
                        parse_cloud_storage_link
                    )(link, procurement_id))

                    eis_procurement_number = parse_result.get("procurement_number")
                    eis_status = "available" if parse_result.get("status") == "success" else "unavailable"
                    eis_error = parse_result.get("error")

                    eis_data = {
                        "eis_procurement_number": str(eis_procurement_number) if eis_procurement_number else None,
                        "eis_link": link,
                        "eis_status": eis_status,
                        "eis_error": eis_error,
                        "checked_at": datetime.datetime.utcnow().isoformat()
                    }

                    save_raw_data(
                        procurement_id=procurement_id,
                        document_type="eis_data",
                        full_analysis=eis_data
                    )
                except Exception as e:
                    logger.error(f"Ошибка обработки ЕИС-ссылки {link}: {e}")
                    eis_data = {
                        "eis_link": link,
                        "eis_status": "error",
                        "eis_error": str(e),
                        "checked_at": datetime.datetime.utcnow().isoformat()
                    }
                    save_raw_data(
                        procurement_id=procurement_id,
                        document_type="eis_data",
                        full_analysis=eis_data
                    )
                continue

            # === Обычные ссылки (не ЕИС) → с метаданными ===
            try:
                metadata = link_to_metadata.get(link) if link_to_metadata else None
                doc_code = metadata[0] if metadata else "unknown"
                doc_label = metadata[1] if metadata else "Неизвестный документ"
                comment = metadata[2] if metadata else None

                parse_result = run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(
                    parse_cloud_storage_link
                )(link, procurement_id))

                if parse_result.get("status") != "success":
                    error_msg = parse_result.get("error", "Неизвестная ошибка парсинга")
                    filename = f"failed_link_{abs(hash(link)) % (10**8)}"
                    full_analysis_with_meta = {
                        "error": error_msg,
                        "document_code": doc_code,
                        "document_label": doc_label,
                        "comment": comment,
                        "original_link": link
                    }
                    save_raw_data(
                        procurement_id=procurement_id,
                        document_type="Дополнительные материалы",
                        full_analysis=full_analysis_with_meta
                    )
                    documents_results.append({
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": filename,
                        "is_valid": False,
                        "error": error_msg,
                        "conclusion": f"Не удалось обработать ссылку: {error_msg}",
                        "source": "external_link",
                        "original_link": link,
                        "document_code": doc_code,
                        "document_label": doc_label,
                        "comment": comment
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

                            full_analysis_with_meta = {
                                **analysis,
                                "document_code": doc_code,
                                "document_label": doc_label,
                                "comment": comment,
                                "original_link": link
                            }
                            save_raw_data(
                                procurement_id=procurement_id,
                                document_type=final_doc_type,
                                full_analysis=full_analysis_with_meta
                            )
                            save_clean_conclusion(
                                procurement_id=procurement_id,
                                document_type=final_doc_type,
                                conclusion=conclusion
                            )

                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": final_doc_type,
                            "filename": filename,
                            "is_valid": is_valid,
                            "error": None,
                            "conclusion": conclusion,
                            "source": file_info.get("source", "external_link"),
                            "original_link": link,
                            "document_code": doc_code,  # Сохраняем код из link_to_metadata
                            "document_label": doc_label,  # Сохраняем label из link_to_metadata
                            "comment": comment  # Сохраняем комментарий из link_to_metadata
                        })

                    except Exception as e:
                        logger.error(f"Ошибка обработки файла из ссылки {link}: {e}")
                        full_analysis_with_meta = {
                            "error": str(e),
                            "document_code": doc_code,
                            "document_label": doc_label,
                            "comment": comment,
                            "original_link": link
                        }
                        save_raw_data(
                            procurement_id=procurement_id,
                            document_type="Дополнительные материалы",
                            full_analysis=full_analysis_with_meta
                        )
                        documents_results.append({
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": str(e),
                            "conclusion": f"Ошибка обработки: {str(e)}",
                            "source": file_info.get("source", "external_link"),
                            "original_link": link,
                            "document_code": doc_code,
                            "document_label": doc_label,
                            "comment": comment
                        })
                    finally:
                        if os.path.exists(file_path):
                            os.unlink(file_path)

            except Exception as e:
                logger.error(f"Критическая ошибка при обработке ссылки {link}: {e}")
                metadata = link_to_metadata.get(link) if link_to_metadata else (None, None, None)
                doc_code, doc_label, comment = metadata if metadata else ("unknown", "Неизвестный документ", None)

                full_analysis_with_meta = {
                    "error": str(e),
                    "document_code": doc_code,
                    "document_label": doc_label,
                    "comment": comment,
                    "original_link": link
                }
                save_raw_data(
                    procurement_id=procurement_id,
                    document_type="Дополнительные материалы",
                    full_analysis=full_analysis_with_meta
                )
                documents_results.append({
                    "procurement_id": procurement_id,
                    "document_type": "Дополнительные материалы",
                    "filename": f"error_link_{abs(hash(link)) % (10**8)}",
                    "is_valid": False,
                    "error": str(e),
                    "conclusion": f"Критическая ошибка: {str(e)}",
                    "source": "external_link",
                    "original_link": link,
                    "document_code": doc_code,
                    "document_label": doc_label,
                    "comment": comment
                })

    # === Финальная оценка ===
    if not document_analysis_results and not any("zakupki.gov.ru" in link for link in all_links):
        raise ValueError("Нет данных для анализа")

    final_evaluation_result = run_async(check_completeness_with_ai(
        procurement_id, 
        expertise_customer=expertise_customer,
        legislation_type=legislation,
        procurement_method=procurement_method
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
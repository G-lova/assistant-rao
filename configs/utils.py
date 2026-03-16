import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile

import pandas as pd
import rarfile
import textract
import zipfile
from typing import Dict, List, Any
from docx import Document
from pptx import Presentation
from pdf2image import convert_from_path
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from configs.config import Config
from configs.logger import get_logger
from evaluate_documents.ocr import ocr_image_with_qwen_vl


config = Config.get_model_config()


logger = get_logger(__name__)



document_analysis_cache = {}

API_KEY_PATTERN = re.compile(r'^[A-Za-z0-9._\-]+$')





class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Промежуточное ПО для аутентификации входящих запросов по API-ключу.

    Проверяет наличие, формат и корректность API-ключа в заголовке X-API-Key.
    Использует безопасное сравнение строк и скрывает внутренние ошибки конфигурации
    от клиента. Любой сбой в процессе проверки приводит к ответу с кодом 401,
    за исключением случая отсутствия ключа в переменных окружения — тогда возвращается 500.

    Args:
        BaseHTTPMiddleware: Базовый класс промежуточного ПО FastAPI.
    """
    async def dispatch(self, request: Request, call_next):
        """
        Обрабатывает входящий запрос, проверяя валидность API-ключа.

        Args:
            request (Request): Входящий HTTP-запрос.
            call_next (_type_): Следующий обработчик в цепочке middleware.

        Returns:
            _type_: Ответ сервера: либо результат следующего обработчика при успешной
                    аутентификации, либо JSONResponse с ошибкой (401 или 500).
        """

        try:
            api_key = request.headers.get("X-API-Key")
            
            # 1. Проверка наличия
            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"}
                )
            
            # 2. Проверка формата (только ASCII)
            if not API_KEY_PATTERN.match(api_key):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"}
                )
            
            # 3. Проверка конфигурации
            expected_api_key = Config.API_KEY
            if not expected_api_key:
                # Логируем внутреннюю ошибку, но не раскрываем клиенту
                print("CRITICAL: API_KEY not set in environment")
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Service misconfigured"}
                )
            
            # 4. Безопасное сравнение
            if not secrets.compare_digest(api_key, expected_api_key):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"}
                )
            
            return await call_next(request)
        
        except Exception as e:
            logger.exception("Неожиданная ошибка в APIKeyMiddleware")
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error during authentication"}
            )




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


def extract_required_docs_from_analysis(document_analysis_results: Dict[str, Dict]) -> List[str]:
    """
    Извлекает список требуемых документов из результатов анализа исходного документа.
    ИСКЛЮЧАЕТ 'проект контракта' из проверки комплектности.

    Args:
        document_analysis_results (Dict[str, Dict]): Словарь с результатами анализа документов.

    Returns:
        List[str]: Уникальный список требуемых документов, исключая 'проект контракта'.
    """
    required_docs = set()
    
    for doc_name, analysis in document_analysis_results.items():
        if "raw_data" in analysis and "attached_documents_list" in analysis["raw_data"]:
            attached_docs = analysis["raw_data"]["attached_documents_list"]
            if attached_docs:
                # ФИЛЬТРУЕМ - исключаем 'проект контракта'
                filtered_docs = [
                    doc for doc in attached_docs 
                    if doc.lower().strip() != "проект контракта"
                ]
                required_docs.update(filtered_docs)
    
    return list(required_docs)


def extract_provided_docs_from_results(documents_results: List[Dict]) -> List[str]:
    """
    Извлекает список предоставленных документов из результатов обработки загрузки.

    Функция собирает типы документов, которые были успешно загружены и обработаны,
    на основе поля 'document_type' в каждом элементе списка.

    Args:
        documents_results (List[Dict]): Список словарей с результатами обработки каждого загруженного документа.

    Returns:
        List[str]: Список типов предоставленных документов (без дубликатов не требуется, сохраняется порядок).
    """
    return [doc["document_type"] for doc in documents_results if doc.get("document_type")]
    

def create_summary_report(
    procurement_id: str,
    documents_results: List[Dict],
    document_analysis_results: Dict[str, Dict],
    completeness_check: Dict,
    consistency_result: Dict
) -> Dict[str, Any]:
    """
    Формирует сводный отчёт по результатам анализа комплекта документов закупки.

    Объединяет данные из нескольких источников: результаты обработки файлов,
    детальный анализ каждого документа, проверку комплектности и согласованности.
    Собирает агрегированные сведения (даты, суммы, юрлица, ссылки на законодательство)
    и рассчитывает статистику по документам.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.
        documents_results (List[Dict]): Список результатов обработки загруженных файлов
                                       (метаданные, статус валидации и т.д.).
        document_analysis_results (Dict[str, Dict]): Результаты детального анализа
                                                    по каждому документу (ключ — имя файла).
        completeness_check (Dict): Результаты проверки полноты комплекта документов.
        consistency_result (Dict): Результаты проверки внутренней согласованности данных.

    Returns:
        Dict[str, Any]: Структурированный сводный отчёт, содержащий:
            - procurement_id и timestamp;
            - documents_summary: краткая информация по каждому документу;
            - completeness_check и consistency_check: статусы и выявленные проблемы;
            - aggregated_data: объединённые данные из всех документов (даты, суммы и др.);
            - statistics: статистика по количеству и типам документов.
    """
    summary = {
        "procurement_id": procurement_id,
        "processing_timestamp": pd.Timestamp.now().isoformat(),
        "documents_summary": {},
        "completeness_check": {
            "status": completeness_check.get("status"),
            "declared_attachments": completeness_check.get("declared_attachments", []),
            "missing_documents": completeness_check.get("missing_in_upload", []),
            "provided_documents": completeness_check.get("provided_documents", [])
        },
        "consistency_check": {
            "status": consistency_result.get("status"),
            "issues": consistency_result.get("issues", [])
        }
    }
    
    # Собираем данные из ВСЕХ документов с сохранением document_code и document_label
    for doc_result in documents_results:
        doc_name = doc_result["filename"]
        doc_summary = {
            # Сохраняем ВСЕ метаданные из эндпойнта
            "document_code": doc_result.get("document_code", "unknown"),
            "document_label": doc_result.get("document_label", "Неизвестный документ"),
            "document_type": doc_result.get("document_type", "Дополнительные материалы"),
            "source": doc_result.get("source", "unknown"),
            "is_valid": doc_result.get("is_valid", False),
            "error": doc_result.get("error"),
            "conclusion": doc_result.get("conclusion"),
            "comment": doc_result.get("comment")
        }
        
        # Добавляем анализ если он есть
        if doc_name in document_analysis_results:
            analysis = document_analysis_results[doc_name]
            doc_summary.update({
                "analysis_status": analysis.get("status", "unknown"),
                "type_compliance": analysis.get("analysis", {}).get("type_compliance", {}),
                "readability": analysis.get("analysis", {}).get("readability", {}),
                "raw_data": analysis.get("analysis", {}).get("raw_data", {}),
                "analysis_conclusion": analysis.get("analysis", {}).get("conclusion", "")
            })
        
        summary["documents_summary"][doc_name] = doc_summary
    
    # Агрегируем ключевые данные из всех документов
    all_dates = []
    all_amounts = []
    all_legal_entities = []
    all_law_references = []
    all_document_codes = []
    all_document_labels = []
    
    for doc_name, analysis in document_analysis_results.items():
        raw_data = analysis.get("raw_data", {})
        
        # Собираем метаданные документов
        doc_result = next((doc for doc in documents_results if doc["filename"] == doc_name), {})
        all_document_codes.append({
            "document_code": doc_result.get("document_code", "unknown"),
            "document_label": doc_result.get("document_label", "Неизвестный документ"),
            "filename": doc_name
        })
        
        # Собираем даты
        if "dates" in raw_data:
            for date_info in raw_data["dates"]:
                date_info["source_document"] = doc_name
                date_info["document_code"] = doc_result.get("document_code", "unknown")
                all_dates.append(date_info)
        
        # Собираем суммы
        if "amounts" in raw_data:
            for amount_info in raw_data["amounts"]:
                amount_info["source_document"] = doc_name
                amount_info["document_code"] = doc_result.get("document_code", "unknown")
                all_amounts.append(amount_info)
        
        # Собираем юридические лица
        if "legal_entities" in raw_data:
            for entity_info in raw_data["legal_entities"]:
                entity_info["source_document"] = doc_name
                entity_info["document_code"] = doc_result.get("document_code", "unknown")
                all_legal_entities.append(entity_info)
        
        # Собираем ссылки на законодательство
        if "law_references" in raw_data:
            for law_ref in raw_data["law_references"]:
                all_law_references.append({
                    "reference": law_ref,
                    "source_document": doc_name,
                    "document_code": doc_result.get("document_code", "unknown")
                })
    
    # Добавляем агрегированные данные в сводный отчет
    summary["aggregated_data"] = {
        "dates": all_dates,
        "amounts": all_amounts,
        "legal_entities": all_legal_entities,
        "law_references": all_law_references,
        "document_metadata": all_document_codes
    }
    
    # Добавляем расширенную статистику
    summary["statistics"] = {
        "total_documents": len(documents_results),
        "valid_documents": len([doc for doc in documents_results if doc.get("is_valid")]),
        "invalid_documents": len([doc for doc in documents_results if not doc.get("is_valid")]),
        "unique_document_types": list(set(doc.get("document_type", "Дополнительные материалы") for doc in documents_results)),
        "unique_document_codes": list(set(doc.get("document_code", "unknown") for doc in documents_results)),
        "total_amounts_found": len(all_amounts),
        "total_dates_found": len(all_dates),
        "total_legal_entities_found": len(all_legal_entities),
        "sources_breakdown": {
            "uploaded_files": len([doc for doc in documents_results if doc.get("source") == "uploaded_file"]),
            "external_links": len([doc for doc in documents_results if doc.get("source") == "external_link"]),
            "eis_data": len([doc for doc in documents_results if doc.get("source") == "eis_data"])
        }
    }
    
    # Добавляем финальную оценку если есть
    if completeness_check:
        summary["final_evaluation"] = {
            "overall_status": completeness_check.get("overall_status"),
            "overall_summary": completeness_check.get("overall_summary"),
            "evaluation_timestamp": pd.Timestamp.now().isoformat()
        }
    
    logger.info(f"Сформирован summary_report для {procurement_id}. Документов: {len(documents_results)}, Кодов: {len(all_document_codes)}")
    return summary



def split_large_text(text: str, max_chunk_size: int = 20000) -> List[str]:
        """
        Разбивает большой текст на фрагменты заданного максимального размера с сохранением смысловой целостности.

        Сначала пытается разделить текст по абзацам, чтобы не разрывать логические блоки.
        Если после разбиения по абзацам получаются слишком крупные фрагменты — переключается
        на разбиение по предложениям. Гарантирует, что каждый фрагмент не превышает `max_chunk_size`
        и при этом максимально сохраняет контекст для последующей обработки (например, LLM).

        Args:
            text (str): Исходный текст, который необходимо разбить на части.
            max_chunk_size (int, optional): Максимально допустимый размер одного фрагмента в символах.

        Returns:
            List[str]: Список строк-фрагментов, каждый из которых имеет длину не более `max_chunk_size`.
                Если исходный текст короче лимита — возвращается список из одного элемента.
                Пустые строки не включаются в результат.
        """
        if len(text) <= max_chunk_size:
            return [text]
        
        # Разделяем по абзацам, если возможно
        paragraphs = text.split('\n\n')
        chunks = []
        current_chunk = ""
        
        for paragraph in paragraphs:
            if len(current_chunk) + len(paragraph) + 2 <= max_chunk_size:
                current_chunk += paragraph + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = paragraph + "\n\n"
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        # Если абзацы слишком большие, разделяем по предложениям
        if not chunks or any(len(chunk) > max_chunk_size * 1.5 for chunk in chunks):
            sentences = re.split(r'[.!?]+', text)
            chunks = []
            current_chunk = ""
            
            for sentence in sentences:
                if len(current_chunk) + len(sentence) + 1 <= max_chunk_size:
                    current_chunk += sentence + '. '
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = sentence + '. '
            
            if current_chunk:
                chunks.append(current_chunk.strip())
        
        return chunks

def extract_json_objects(text: str):
    """Возвращает список всех JSON-подобных объектов с учётом вложенности."""
    objects = []
    stack = []
    start_index = None

    for i, char in enumerate(text):
        if char == '{':
            if not stack:
                start_index = i
            stack.append('{')
        elif char == '}':
            if stack:
                stack.pop()
                if not stack and start_index is not None:
                    objects.append(text[start_index:i+1])
                    start_index = None
    return objects
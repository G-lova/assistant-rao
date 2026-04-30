import magic
import os
import pandas as pd
import re
import secrets
import tiktoken

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse, parse_qs

from configs.config import Config
from configs.logger import get_logger


config = Config.get_model_config()
logger = get_logger(__name__)


document_analysis_cache = {}

API_KEY_PATTERN = re.compile(r'^[A-Za-z0-9._\-]+$')


# Глобальный энкодер (инициализируется один раз при старте модуля)
try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")  # GPT-3.5/4, Qwen, большинство OpenAI-совместимых
except Exception:
    _ENCODER = tiktoken.get_encoding("p50k_base")   # Fallback

# Единая карта MIME → расширение (используется во всём проекте)
MIME_TO_EXT_MAP = {
    'application/pdf': '.pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-excel': '.xls',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.ms-powerpoint': '.ppt',
    'text/plain': '.txt',
    'text/csv': '.csv',
    'text/html': '.html',
    'application/json': '.json',
    'application/xml': '.xml',
    'text/xml': '.xml',
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/svg+xml': '.svg',
    'application/zip': '.zip',
    'application/x-rar-compressed': '.rar',
    'application/x-7z-compressed': '.7z',
    'application/gzip': '.gz',
    'application/x-tar': '.tar',
    'application/octet-stream': '.bin',
    'application/download': '.bin',
}




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

def get_file_extension(
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    content: Optional[bytes] = None,
    url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None
) -> str:
    """
    Универсальное определение расширения файла.
    Проверяет источники в порядке убывания надёжности.
    
    Приоритет:
    1. Явное имя файла (filename)
    2. Заголовок Content-Type или явно переданный MIME
    3. Магические байты содержимого (python-magic)
    4. Путь или параметры URL
    5. Fallback: '.bin'
    """
    # По имени файла (самый надёжный, если передан явно)
    if filename and '.' in filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext:
            return ext

    # По Content-Type
    ct = content_type or (headers.get('Content-Type', '') if headers else '')
    if ct:
        mime = ct.split(';')[0].strip().lower()
        if ext := MIME_TO_EXT_MAP.get(mime):
            return ext

    # По содержимому (magic bytes)
    if content:
        try:
            mime = magic.Magic(mime=True).from_buffer(content)
            if ext := MIME_TO_EXT_MAP.get(mime):
                return ext
        except Exception:
            pass  # Игнорируем ошибки magic, идём дальше

    # По URL (путь или query-параметр filename)
    if url:
        parsed = urlparse(url)
        # Проверяем расширение в пути
        path = parsed.path.lower()
        if '.' in path:
            ext = os.path.splitext(path)[1].lower()
            if ext:
                return ext
        # Проверяем query-параметр ?filename=...
        params = parse_qs(parsed.query)
        if 'filename' in params:
            fname = params['filename'][0]
            if '.' in fname:
                ext = os.path.splitext(fname)[1].lower()
                if ext:
                    return ext

    # Fallback
    return '.bin'




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

def split_large_text(text: str, max_chunk_size: int = 10000) -> List[str]:
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
        if not text:
            return []
            
        # Быстрая проверка для коротких текстов
        if len(_ENCODER.encode(text)) <= max_chunk_size:
            return [text]

        # Нормализуем переносы строк
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        paragraphs = [p for p in re.split(r'\n{2,}', text) if p.strip()]
        
        chunks = []
        current_tokens = []

        def flush():
            """Сбрасывает накопленные токены в новый чанк"""
            nonlocal current_tokens
            if current_tokens:
                chunks.append(_ENCODER.decode(current_tokens).strip())
                current_tokens = []

        for paragraph in paragraphs:
            p_tokens = _ENCODER.encode(paragraph)
            
            # 1. Если абзац влезает в текущий чанк -> добавляем
            if len(current_tokens) + len(p_tokens) <= max_chunk_size:
                current_tokens.extend(p_tokens)
                continue
                
            # 2. Не влезает -> закрываем текущий чанк
            flush()
            
            # 3. Если абзац всё ещё больше лимита -> делим на предложения
            if len(p_tokens) > max_chunk_size:
                sentences = re.split(r'(?<=[.!?…])\s+', paragraph)
                sentences = [s.strip() for s in sentences if s.strip()]
                
                for sentence in sentences:
                    s_tokens = _ENCODER.encode(sentence)
                    
                    if len(current_tokens) + len(s_tokens) > max_chunk_size:
                        flush()
                        # 4. Fallback: если предложение гигантское -> рубим по токенам
                        if len(s_tokens) > max_chunk_size:
                            for i in range(0, len(s_tokens), max_chunk_size):
                                chunks.append(_ENCODER.decode(s_tokens[i:i+max_chunk_size]).strip())
                        else:
                            current_tokens.extend(s_tokens)
                    else:
                        current_tokens.extend(s_tokens)
            else:
                # Абзац гарантированно влезет в пустой чанк
                current_tokens.extend(p_tokens)
                
        flush()
        return [c for c in chunks if c]

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
import os
import logging
import json
import re
import asyncio

from typing import List, Dict, Any
from openai import OpenAI

from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.prompts import (RESPONSE_JSON_SCHEMA,
                         SYSTEM_PROMPT,
                         TYPE_DETECTION_PROMPT,
                         SYSTEM_FINAL_EVALUATION_PROMPT,
                         USER_FINAL_EVALUATION_PROMPT,
                         FINAL_EVALUATION_SCHEMA)
from configs.working_with_db import get_raw_data_by_procurement_id
from configs.config import Config


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Это к Qwen/Qwen2.5-VL-7B-Instruct-AWQ
#client = OpenAI(
#    base_url=Config.M_MODEL_API_URL,
#    api_key=Config.M_MODEL_API_KEY
#)
#model_name = Config.M_MODEL_NAME


# Это к Qwen/Qwen2.5-14B-Instruct-AWQ
client = OpenAI(
    base_url=Config.MODEL_API_URL,
    api_key=Config.MODEL_API_KEY
)
model_name = Config.MODEL_NAME


ALLOWED_DOC_TYPES = list(DOCUMENT_TYPE_MAPPING.keys())


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


async def analyze_document_chunks(
    chunks: List[str],
    document_name: str,
    document_type: str,
    law_type: str,
    procurement_method: str
) -> Dict[str, Any]:
    """
    Асинхронно анализирует документ, разбитый на фрагменты, с определением типа и объединением результатов.

    Функция сначала определяет реальный тип документа по первому фрагменту (до 1500 символов),
    затем параллельно обрабатывает все фрагменты с использованием этого типа. После получения
    результатов по каждому блоку они объединяются в единый отчёт. Поддерживает устойчивость к ошибкам:
    если часть блоков не удалось проанализировать — результаты успешных всё равно учитываются.

    Args:
        chunks (List[str]): Список текстовых фрагментов документа, полученных в результате разбиения.
        document_name (str): Оригинальное имя документа (для логирования и идентификации).
        document_type (str): Ожидаемый тип документа (используется как fallback, если автоматическое определение не удалось).
        law_type (str): Нормативная база (например, "44-ФЗ"), влияющая на критерии анализа.
        procurement_method (str): Способ закупки (например, "Конкурс"), используемый для контекстной оценки.

    Returns:
        Dict[str, Any]: Словарь с итоговым результатом анализа, содержащий:
            - status (str): "success" или "error".
            - analysis (dict): Объединённые данные по всем блокам, включая:
                - type_compliance: Соответствие типа (фактический и ожидаемый типы выставлены в соответствии с определённым значением).
                - readability: Оценка читаемости.
                - raw_data: Структурированные извлечённые данные (объединённые).
                - conclusion: Итоговое заключение.
            При полной неудаче возвращается объект с описанием ошибки.
    """
    if not chunks:
        return {
            "status": "error",
            "analysis": {
                "type_compliance": {"status": "не соответствует", "issues": ["Пустой документ"]},
                "readability": {"status": "неудовлетворительно", "issues": ["Пустой документ"]},
                "raw_data": {},
                "conclusion": "Документ пуст или не содержит читаемого текста"
            }
        }
    
    # ЭТАП 1: Определение типа документа по первым 1500 символам
    first_n_chars = chunks[0][:1500]
    detected_type = await detect_document_type(first_n_chars, document_name)
    
    # Если тип не определен, используем исходный или "Дополнительные материалы"
    final_document_type = detected_type if detected_type != "Дополнительные материалы" else document_type
    if not final_document_type:
        final_document_type = "Дополнительные материалы"
    
    logger.info(f"Финальный тип для анализа: '{final_document_type}'")
    
    # ЭТАП 2: Параллельный анализ всех чанков
    tasks = []
    for i, chunk in enumerate(chunks):
        task_name = f"{document_name} (блок {i+1})" if len(chunks) > 1 else document_name
        task = analyze_single_document(
            content=chunk,
            document_name=task_name,
            document_type=final_document_type,  # Используем ОПРЕДЕЛЕННЫЙ тип
            law_type=law_type,
            procurement_method=procurement_method
        )
        tasks.append(task)
    
    # Запускаем все задачи параллельно
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Обрабатываем результаты
    successful_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Ошибка при анализе блока {i+1}: {result}")
            continue
        if result["status"] == "success":
            # Принудительно устанавливаем определенный тип
            result["analysis"]["type_compliance"]["actual_type"] = final_document_type
            result["analysis"]["type_compliance"]["expected_type"] = final_document_type
            successful_results.append(result)
    
    if not successful_results:
        return {
            "status": "error",
            "analysis": {
                "type_compliance": {"status": "не соответствует", "issues": ["Все блоки не удалось проанализировать"]},
                "readability": {"status": "неудовлетворительно", "issues": ["Ошибка анализа всех блоков"]},
                "raw_data": {},
                "conclusion": "Не удалось проанализировать ни один блок документа"
            }
        }
    
    # Объединяем результаты
    final_result = merge_analysis_results(successful_results)
    return final_result


def merge_analysis_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Объединяет несколько результатов анализа документа в единый согласованный вывод.

    Функция принимает список результатов, полученных при анализе отдельных фрагментов одного документа,
    и сливает их содержимое (в первую очередь — извлечённые данные) в один итоговый объект.
    Сохраняет основной статус и типовой вывод из первого блока, но дополняет его информацией из последующих.

    Args:
        results (List[Dict[str, Any]]): Список словарей с результатами анализа, каждый из которых имеет структуру:
            {
                "status": "success",
                "analysis": {
                    "type_compliance": { ... },
                    "readability": { ... },
                    "raw_data": { ... },
                    "conclusion": "..."
                }
            }

    Returns:
        Dict[str, Any]: Единый результат анализа со следующими особенностями:
            - status: берётся из первого результата (предполагается, что он успешен).
            - raw_data: объединены списки `dates`, `amounts`, `legal_entities`, `law_references` и 
              уникализирован список приложенных документов (`attached_documents_list`).
            - conclusion: дополнен уточнением о пофрагментном анализе.
            Если список пуст — возвращается шаблон с ошибкой.
    """
    if not results:
        return {
            "status": "error",
            "analysis": {
                "type_compliance": {"status": "не соответствует", "issues": ["Нет результатов анализа"]},
                "readability": {"status": "неудовлетворительно", "issues": ["Нет результатов анализа"]},
                "raw_data": {},
                "conclusion": "Не удалось проанализировать документ"
            }
        }
    
    base_result = results[0]
    if len(results) == 1:
        return base_result
    
    # Объединяем raw_data из всех результатов
    merged_raw_data = base_result["analysis"]["raw_data"].copy()
    
    for result in results[1:]:
        if result["status"] == "success":
            # Объединяем списки данных
            for key in ["dates", "amounts", "legal_entities", "law_references"]:
                if key in result["analysis"]["raw_data"]:
                    merged_raw_data[key] = merged_raw_data.get(key, []) + result["analysis"]["raw_data"][key]
            
            # Объединяем attached_documents_list
            if "attached_documents_list" in result["analysis"]["raw_data"]:
                current_list = merged_raw_data.get("attached_documents_list", [])
                new_items = result["analysis"]["raw_data"]["attached_documents_list"]
                merged_raw_data["attached_documents_list"] = list(set(current_list + new_items))
    
    base_result["analysis"]["raw_data"] = merged_raw_data
    
    # Обновляем заключение
    if len(results) > 1:
        base_result["analysis"]["conclusion"] = (
            base_result["analysis"]["conclusion"]
        )
    
    return base_result


async def analyze_single_document(
    content: str,
    document_name: str,
    document_type: str,
    law_type: str,
    procurement_method: str
) -> Dict[str, Any]:
    """
    Анализирует отдельный документ с использованием языковой модели и возвращает структурированный результат.

    Функция обрабатывает текст документа, ограничивает его длину при необходимости,
    формирует системный промпт с учетом типа документа и отправляет запрос к LLM.
    Ответ модели парсится как JSON, при ошибках применяются механизмы восстановления.
    Возвращается унифицированная структура анализа, включающая соответствие типу,
    читаемость, извлеченные данные и общее заключение.

    Args:
        content (str): Текстовое содержимое документа для анализа.
        document_name (str): Имя или идентификатор документа (для логирования и отчетности).
        document_type (str): Ожидаемый тип документа (например, "Извещение", "Документация").
        law_type (str): Тип законодательства, применимого к документу (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ закупки (например, "Электронный аукцион", "Запрос котировок").

    Returns:
        Dict[str, Any]: Словарь с результатом анализа, содержащий:
            - "status": статус выполнения ("success" или ошибка),
            - "document_name": имя документа,
            - "document_type": тип документа,
            - "analysis": детализированный анализ, включающий:
                - "type_compliance": соответствие заявленному типу,
                - "readability": оценка читаемости,
                - "raw_data": извлеченные сущности (даты, суммы, организации и т.д.),
                - "conclusion": итоговое заключение модели.
            В случае таймаута или ошибки возвращается заглушка с минимальной информацией.
    """
    try:
        logger.info(f"Анализ чанка '{document_name}' с типом '{document_type}'")
        logger.info(f"Длина контента: {len(content)} символов")

        # Ограничиваем размер контента для предотвращения ошибок
        if len(content) > 15000:
            content = content[:15000] + "... [контент обрезан]"
            logger.info(f"Контент обрезан до {len(content)} символов")

        # Формируем промпт с подстановкой УЖЕ ИЗВЕСТНОГО типа
        prompt = SYSTEM_PROMPT.format(document_type=document_type)

        # Вызов модели с guided_json и таймаутом
        try:
            response = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.chat.completions.create(
                        model=model_name,
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": f"Проанализируй следующий документ типа '{document_type}':\n\n{content}"}
                        ],
                        extra_body={"guided_json": RESPONSE_JSON_SCHEMA},
                        max_tokens=3000,
                        temperature=0.1
                    )
                ),
                timeout=300.0
            )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при анализе {document_name}")
            # Возвращаем базовый результат вместо исключения
            return create_timeout_analysis(document_name, document_type)

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Получен ответ длиной {len(raw_response)} символов")

        # Парсим JSON с улучшенной обработкой ошибок
        result = None
        try:
            result = json.loads(raw_response)
        except json.JSONDecodeError as e:
            logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")
            
            # Попробуем найти JSON в тексте
            json_pattern = r'\{[^{}]*\{[^{}]*\}[^{}]*\}'  # Ищем вложенные структуры
            matches = re.findall(json_pattern, raw_response)
            
            if matches:
                # Берем самый длинный найденный JSON
                longest_match = max(matches, key=len)
                try:
                    result = json.loads(longest_match)
                    logger.info("JSON найден с помощью regex")
                except json.JSONDecodeError:
                    pass
            
            # Если все еще не получилось, создаем базовую структуру
            if not result:
                logger.error(f"Не удалось распарсить JSON, создаем базовую структуру. Ошибка: {e}")
                result = create_fallback_analysis(document_type, content)

        # Устанавливаем известный тип
        type_compliance = result.get("type_compliance", {})
        type_compliance["actual_type"] = document_type
        type_compliance["expected_type"] = document_type
        
        # Упрощенная проверка соответствия
        if document_type != "Дополнительные материалы":
            type_compliance["status"] = "соответствует"
            type_compliance["issues"] = []
            type_compliance["confidence"] = 0.9
        else:
            type_compliance["status"] = "не соответствует"
            type_compliance["issues"] = ["Тип документа не определен"]
            type_compliance["confidence"] = 0.3

        return {
            "status": "success",
            "document_name": document_name,
            "document_type": document_type,
            "analysis": {
                "type_compliance": type_compliance,
                "readability": result.get("readability", {
                    "status": "удовлетворительно",
                    "issues": []
                }),
                "raw_data": result.get("raw_data", {
                    "dates": [],
                    "amounts": [],
                    "legal_entities": [],
                    "contract_number": "",
                    "law_references": [],
                    "attached_documents_list": []
                }),
                "conclusion": result.get("conclusion", f"Документ типа '{document_type}' проанализирован.")
            }
        }

    except Exception as e:
        logger.error(f"Ошибка при анализе {document_name}: {str(e)}")
        return create_error_analysis(document_name, document_type, str(e))


def create_timeout_analysis(document_name: str, document_type: str) -> Dict[str, Any]:
    """
    Создает заглушку результата анализа для случая таймаута при обработке документа.

    Эта функция возвращает структурированный ответ, имитирующий успешный анализ,
    но с пометкой об ошибке и информацией о прерывании из-за превышения времени ожидания.
    Используется как fallback при возникновении asyncio.TimeoutError в основном анализаторе.

    Args:
        document_name (str): Имя или идентификатор документа.
        document_type (str): Тип документа, указанный при вызове анализа.

    Returns:
        Dict[str, Any]: Словарь с результатом анализа, содержащий:
            - "status": "error" (указывает на незавершённую обработку),
            - "document_name": имя документа,
            - "document_type": тип документа,
            - "analysis": частично заполненная структура анализа с пометкой о таймауте,
              включая минимальные значения по умолчанию для всех полей.
    """
    return {
        "status": "error",
        "document_name": document_name,
        "document_type": document_type,
        "analysis": {
            "type_compliance": {
                "status": "соответствует",
                "issues": ["Таймаут при анализе"],
                "expected_type": document_type,
                "actual_type": document_type,
                "confidence": 0.5
            },
            "readability": {
                "status": "удовлетворительно",
                "issues": []
            },
            "raw_data": {
                "dates": [],
                "amounts": [],
                "legal_entities": [],
                "contract_number": "",
                "law_references": [],
                "attached_documents_list": []
            },
            "conclusion": f"Анализ документа '{document_name}' не завершен из-за таймаута. Рекомендуется проверить документ вручную."
        }
    }


def create_fallback_analysis(document_type: str, content: str) -> Dict[str, Any]:
    """
    Создаёт резервный (fallback) результат анализа документа при сбое основного процесса.

    Используется в случаях, когда основной парсинг или обработка LLM-ответа завершились ошибкой.
    Возвращает структурированный словарь с минимально допустимыми данными: 
    тип документа считается соответствующим, читаемость — удовлетворительной,
    а извлечённые данные — пустыми. В заключении указывается, что анализ выполнен с ограничениями.

    Args:
        document_type (str): Ожидаемый или заявленный тип документа (например, "Техническое задание").
        content (str): Исходный текст документа (может использоваться в будущем для расширенной обработки).

    Returns:
        Dict[str, Any]: Словарь с резервным результатом анализа, содержащий:
            - type_compliance: информация о соответствии типа (статус "соответствует", доверие 0.8),
            - readability: оценка читаемости ("удовлетворительно"),
            - raw_data: пустые списки и поля для дат, сумм, юрлиц и т.д.,
            - conclusion: пояснительное сообщение о технических проблемах.
    """
    return {
        "type_compliance": {
            "status": "соответствует",
            "issues": [],
            "expected_type": document_type,
            "actual_type": document_type,
            "confidence": 0.8
        },
        "readability": {
            "status": "удовлетворительно",
            "issues": []
        },
        "raw_data": {
            "dates": [],
            "amounts": [],
            "legal_entities": [],
            "contract_number": "",
            "law_references": [],
            "attached_documents_list": []
        },
        "conclusion": f"Документ типа '{document_type}' проанализирован. Возникли технические проблемы с парсингом ответа."
    }


def create_error_analysis(document_name: str, document_type: str, error: str) -> Dict[str, Any]:
    """
    Формирует структурированный отчёт об ошибке при анализе документа.

    Используется для возврата унифицированного ответа в случае сбоя при обработке документа.
    Содержит информацию о документе, типе ошибки и заполненные заглушки для всех обязательных полей,
    чтобы обеспечить совместимость с остальной системой обработки результатов.

    Args:
        document_name (str): Имя обрабатываемого файла или документа.
        document_type (str): Ожидаемый тип документа (например, "Извещение", "Техническое задание").
        error (str): Текстовое описание ошибки, произошедшей при анализе.

    Returns:
        Dict[str, Any]: Словарь с полным отчётом об ошибке, содержащий:
            - status: "error" — указывает на неуспешную обработку,
            - document_name и document_type — метаданные документа,
            - analysis: детализированный результат с нулевой уверенностью, пустыми данными и пояснением ошибки.
    """
    return {
        "status": "error",
        "document_name": document_name,
        "document_type": document_type,
        "analysis": {
            "type_compliance": {
                "status": "не соответствует",
                "issues": [f"Ошибка анализа: {error}"],
                "expected_type": document_type,
                "actual_type": document_type,
                "confidence": 0.0
            },
            "readability": {
                "status": "неудовлетворительно",
                "issues": ["Ошибка обработки"]
            },
            "raw_data": {
                "dates": [],
                "amounts": [],
                "legal_entities": [],
                "contract_number": "",
                "law_references": [],
                "attached_documents_list": []
            },
            "conclusion": f"Анализ не выполнен: {error}"
        }
    }


def convert_consistency_result(ai_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Преобразует результат анализа согласованности от ИИ в унифицированный формат ответа.

    Функция маппит статусы согласованности, фильтрует ложные срабатывания (например,
    "одинаковые данные", "отсутствие данных", "разный формат"), объединяет все выявленные
    несоответствия в единый список с указанием уровня серьёзности и формирует итоговое
    заключение. Также сохраняет исходный результат ИИ в поле detailed_result для отладки.

    Args:
        ai_result (Dict[str, Any]): Сырой результат от модуля ИИ, содержащий поля
            consistency_status, critical_issues, medium_issues, minor_issues и overall_assessment.

    Returns:
        Dict[str, Any]: Преобразованный результат в стандартном формате, включающий:
            - "status": общий статус ("ok", "warning", "error"),
            - "issues": список реальных несоответствий с полями field, problem, details, severity,
            - "conclusion": человекочитаемое заключение,
            - "detailed_result": исходный ответ ИИ (для аудита и отладки).
    """
    # Преобразуем статус
    status_map = {
        "согласовано": "ok",
        "частично согласовано": "warning", 
        "не согласовано": "error"
    }
    
    consistency_status = ai_result.get("consistency_status", "согласовано")
    status = status_map.get(consistency_status, "ok")
    
    # Объединяем все issues в единый список для обратной совместимости
    all_issues = []
    
    # Преобразуем critical_issues (только реальные расхождения)
    for issue in ai_result.get("critical_issues", []):
        # ФИЛЬТРУЕМ: не добавляем мнимые расхождения
        description = issue.get("description", "").lower()
        if any(keyword in description for keyword in ["одинаковые данные", "отсутствие данных", "разный формат"]):
            continue  # Пропускаем мнимые расхождения
            
        all_issues.append({
            "field": "комплексный анализ",
            "problem": issue.get("description", ""),
            "details": f"Документы: {', '.join(issue.get('affected_documents', []))}. {issue.get('evidence', '')}",
            "severity": "high"
        })
    
    # Преобразуем medium_issues
    for issue in ai_result.get("medium_issues", []):
        description = issue.get("description", "").lower()
        if any(keyword in description for keyword in ["одинаковые данные", "отсутствие данных", "разный формат"]):
            continue
            
        all_issues.append({
            "field": "комплексный анализ", 
            "problem": issue.get("description", ""),
            "details": f"Документы: {', '.join(issue.get('affected_documents', []))}. {issue.get('evidence', '')}",
            "severity": "medium"
        })
    
    # Преобразуем minor_issues (обычно это нормально)
    for issue in ai_result.get("minor_issues", []):
        all_issues.append({
            "field": "комплексный анализ",
            "problem": issue.get("description", ""),
            "details": f"Документы: {', '.join(issue.get('affected_documents', []))}",
            "severity": "low"
        })
    
    # Формируем заключение на основе РЕАЛЬНЫХ расхождений
    overall_assessment = ai_result.get("overall_assessment", {})
    conclusion = overall_assessment.get("summary", "Проверка согласованности завершена.")
    
    real_issues_count = len(all_issues)
    if real_issues_count == 0:
        conclusion = "Документы согласованы. Критических расхождений не обнаружено."
        status = "ok"
    else:
        critical_count = len([issue for issue in all_issues if issue.get("severity") == "high"])
        conclusion = f"Обнаружено расхождений: {real_issues_count} (критических: {critical_count})."
    
    return {
        "status": status,
        "issues": all_issues,
        "conclusion": conclusion,
        "detailed_result": ai_result
    }


def create_fallback_consistency_result() -> Dict[str, Any]:
    """
    Создаёт резервный (заглушечный) результат проверки согласованности при возникновении ошибки.

    Возвращается в случаях, когда основной анализ с помощью ИИ недоступен — например,
    из-за таймаута, ошибки парсинга или сбоя в работе модели. Результат помечен как
    "согласовано", но содержит поясняющее сообщение о невозможности выполнить проверку.

    Returns:
        Dict[str, Any]: Стандартизированный словарь с нейтральным статусом согласованности
            и информацией о том, что проверка не была выполнена. Содержит пустые списки
            для всех категорий несоответствий и пояснение в overall_assessment.
    """
    return {
        "consistency_status": "согласовано",
        "critical_issues": [],
        "medium_issues": [],
        "minor_issues": [],
        "overall_assessment": {
            "summary": "Целостная проверка согласованности не выполнена из-за технической ошибки",
            "total_issues_found": "0",
            "critical_issues_count": "0", 
            "consistency_score": "не определена",
            "recommendations": "Рекомендуется повторить проверку"
        }
    }


def prepare_documents_data_for_consistency_check(raw_data: Dict[str, Any]) -> str:
    """
    Подготавливает и фильтрует данные документов для последующей проверки согласованности.

    Исключает документ типа "Проект контракта", извлекает только ключевые поля
    (номер контракта, даты, суммы, юридические лица, ссылки на законодательство)
    и форматирует их в компактную текстовую строку, пригодную для передачи в ИИ-модель.
    Пустые или несущественные данные отбрасываются, а объём каждого поля ограничен
    для оптимизации контекста модели.

    Args:
        raw_data (Dict[str, Any]): Словарь с необработанными данными документов,
            где ключи — типы документов, а значения — их содержимое и метаданные.

    Returns:
        str: Отформатированная строка с ключевыми данными из всех документов,
            за исключением "Проекта контракта", готовая к использованию в промпте ИИ.
    """
    documents_data = []
    
    for doc_type, doc_data in raw_data.items():
        if not isinstance(doc_data, dict):
            continue
            
        # ИСКЛЮЧАЕМ "Проект контракта" из проверки согласованности
        if doc_type.lower().strip() == "проект контракта":
            logger.info(f"Исключен из проверки согласованности: {doc_type}")
            continue
            
        doc_info = {
            "document_name": doc_type,
            "key_data": {}
        }
        
        # Извлекаем только КЛЮЧЕВЫЕ данные для анализа
        raw_data_content = doc_data.get("raw_data", {})
        
        # Только самые важные поля для проверки согласованности
        key_fields = {
            "contract_number": raw_data_content.get("contract_number"),
            "dates": raw_data_content.get("dates", [])[:3],
            "amounts": raw_data_content.get("amounts", [])[:3],
            "legal_entities": raw_data_content.get("legal_entities", [])[:2],
            "law_references": raw_data_content.get("law_references", [])[:5]
        }
        
        # Добавляем только непустые поля
        for field, value in key_fields.items():
            if value and (isinstance(value, list) and value or isinstance(value, str) and value.strip()):
                doc_info["key_data"][field] = value
        
        if doc_info["key_data"]:  # Добавляем только если есть ключевые данные
            documents_data.append(doc_info)
    
    # Форматируем компактно для LLM
    formatted_data = "КЛЮЧЕВЫЕ ДАННЫЕ ИЗ ДОКУМЕНТОВ (Проект контракта исключен):\n\n"
    for doc in documents_data:
        formatted_data += f"=== {doc['document_name']} ===\n"
        
        key_data = doc['key_data']
        for key, value in key_data.items():
            formatted_data += f"{key}: {str(value)[:200]}\n"
        
        formatted_data += "\n"
    
    logger.info(f"Подготовлено данных для проверки согласованности (без Проекта контракта): {len(formatted_data)} символов")
    logger.info(f"Документы для проверки: {[doc['document_name'] for doc in documents_data]}")
    return formatted_data


def normalize_document_type(doc_type: str) -> str:
    """
    Нормализует тип документа, проверяя его наличие в списке разрешённых значений.

    Функция очищает входную строку от лишних пробелов и сравнивает её с предопределённым
    списком допустимых типов документов (`ALLOWED_DOC_TYPES`). Если точное совпадение найдено,
    возвращается соответствующий тип. В противном случае — значение по умолчанию.

    Args:
        doc_type (str): Исходное название типа документа, полученное, например, из метаданных или от пользователя.

    Returns:
        str: Нормализованный тип документа из списка `ALLOWED_DOC_TYPES`, 
             либо "Дополнительные материалы", если входной тип не распознан или недопустим.
    """
    if not doc_type or not isinstance(doc_type, str):
        return "Дополнительные материалы"
    
    # Простая очистка
    doc_type_clean = doc_type.strip()
    
    # Проверяем, есть ли тип в разрешенных
    for allowed_type in ALLOWED_DOC_TYPES:
        if doc_type_clean == allowed_type:
            return allowed_type
    
    # Если тип не найден в разрешенных, возвращаем дополнительные материалы
    return "Дополнительные материалы"


async def detect_document_type(first_n_chars: str, document_name: str) -> str:
    """
    Определяет тип документа по его начальному фрагменту с помощью языковой модели.

    Функция отправляет первые 1500 символов текста в LLM с инструкцией идентифицировать тип
    из предопределённого списка допустимых значений (`ALLOWED_DOC_TYPES`). Используется
    guided JSON для обеспечения строгого формата ответа. При ошибках, таймаутах или неудачном
    парсинге возвращается тип по умолчанию.

    Args:
        first_n_chars (str): Начальный фрагмент текста документа (обычно первые несколько тысяч символов).
        document_name (str): Имя файла или документа (используется для логирования).

    Returns:
        str: Определённый тип документа из списка `ALLOWED_DOC_TYPES` (например, "Извещение", "Техническое задание").
             Если тип не удалось определить — возвращается "Дополнительные материалы".
    """
    try:
        logger.info(f"Определение типа документа: {document_name}")
        
        # ИСПОЛЬЗУЕМ СЫРОЙ ТЕКСТ БЕЗ ОЧИСТКИ
        text_for_analysis = first_n_chars[:1500]  # Ограничиваем объем для скорости
        
        if not text_for_analysis.strip():
            logger.warning(f"Текст пустой для документа {document_name}")
            return "Дополнительные материалы"
        
        # Создаем упрощенную JSON схему для определения типа
        type_detection_schema = {
            "type": "object",
            "properties": {
                "document_type": {
                    "type": "string",
                    "enum": ALLOWED_DOC_TYPES,
                    "description": "Тип документа определенный по началу текста"
                }
            },
            "required": ["document_type"],
            "additionalProperties": False
        }
        
        # Вызываем модель с guided JSON для точного определения типа
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system", 
                            "content": TYPE_DETECTION_PROMPT
                        },
                        {
                            "role": "user", 
                            "content": f"Определи тип этого документа:\n\n{text_for_analysis}"
                        }
                    ],
                    max_tokens=100,
                    temperature=0.1,
                    extra_body={"guided_json": type_detection_schema}
                )
            ),
            timeout=300.0
        )
        
        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Сырой ответ модели: {raw_response}")
        
        # Парсим JSON ответ
        try:
            result = json.loads(raw_response)
            detected_type = result.get("document_type", "Дополнительные материалы")
            logger.info(f"Модель определила тип: '{detected_type}' для документа '{document_name}'")
            return detected_type
            
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON ответа: {e}")
            # Пробуем извлечь тип вручную из ответа
            for doc_type in ALLOWED_DOC_TYPES:
                if doc_type in raw_response:
                    logger.info(f"Найден тип вручную: '{doc_type}'")
                    return doc_type
            return "Дополнительные материалы"
        
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при определении типа документа {document_name}")
        return "Дополнительные materiales"
    except Exception as e:
        logger.error(f"Ошибка при определении типа документа {document_name}: {str(e)}")
        return "Дополнительные материалы"


def create_fallback_completeness_result(required_docs: List[str], provided_docs: List[str]) -> Dict[str, Any]:
    """
    Создаёт резервный результат проверки комплектности документов при сбое основного анализа.

    Выполняет упрощённую проверку: исключает "Проект контракта" из списка обязательных
    документов, сопоставляет предоставленные документы с требуемыми по ключевым словам
    и определяет отсутствующие. Используется как заглушка, когда основной ИИ-анализ
    недоступен или завершился ошибкой.

    Args:
        required_docs (List[str]): Список наименований обязательных документов.
        provided_docs (List[str]): Список наименований фактически предоставленных документов.

    Returns:
        Dict[str, Any]: Словарь с результатом проверки комплектности, содержащий:
            - "completeness_status": "полный" или "неполный",
            - "missing_documents": список недостающих документов (без "Проекта контракта"),
            - "document_mapping": соответствие между требуемыми и найденными документами,
            - "reasoning": пояснение, что проверка выполнена базовым методом.
    """
    # ФИЛЬТРУЕМ - исключаем "Проект контракта" из обязательных документов
    filtered_required_docs = [
        doc for doc in required_docs 
        if doc.lower().strip() != "проект контракта"
    ]
    
    missing_docs = []
    document_mapping = {}
    
    # Базовое сопоставление по ключевым словам (исключая "Проект контракта")
    for req_doc in filtered_required_docs:
        found_match = False
        for prov_doc in provided_docs:
            if is_document_match(req_doc, prov_doc):
                document_mapping[req_doc] = prov_doc
                found_match = True
                break
                
        if not found_match:
            missing_docs.append(req_doc)
    
    status = "полный" if not missing_docs else "неполный"
    
    return {
        "completeness_status": status,
        "missing_documents": missing_docs,
        "document_mapping": document_mapping,
        "reasoning": "Проверка выполнена базовым методом. Проект контракта исключен из проверки комплектности."
    }


def is_document_match(required_doc: str, provided_doc: str) -> bool:
    """
    Проверяет, соответствует ли предоставленный документ требуемому с учётом синонимов и групп эквивалентности.

    Функция игнорирует регистр и пробелы, исключает "Проект контракта" из сопоставления,
    а также использует предопределённые группы эквивалентных наименований документов
    (например, "ТЗ" и "Техническое задание"). Поддерживает точное, частичное и групповое совпадение.

    Args:
        required_doc (str): Наименование обязательного документа.
        provided_doc (str): Наименование предоставленного документа.

    Returns:
        bool: True, если документы считаются соответствующими, иначе False.
    """
    # ЕСЛИ ТРЕБУЕМЫЙ ДОКУМЕНТ - "ПРОЕКТ КОНТРАКТА", СРАЗУ ВОЗВРАЩАЕМ False
    if required_doc.lower().strip() == "проект контракта":
        return False
        
    required_lower = required_doc.lower()
    provided_lower = provided_doc.lower()
    
    # Группы эквивалентных документов (БЕЗ ГРУППЫ ПРОЕКТА КОНТРАКТА)
    price_group = ["обоснование н(м)цк", "обоснование цены", "расчет нмцк", "смета"]
    tech_group = ["техническое задание", "тз", "описание объекта закупки"]
    notice_group = ["извещение", "уведомление о закупке"]
    requirements_group = ["требования к содержанию заявки", "инструкция для участников"]
    
    # Проверка принадлежности к одной группе
    groups = [price_group, tech_group, notice_group, requirements_group]
    
    for group in groups:
        if required_lower in group and any(doc in provided_lower for doc in group):
            return True
            
    # Точноет совпадение
    if required_lower == provided_lower:
        return True
        
    # Частичное совпадение
    if required_lower in provided_lower or provided_lower in required_lower:
        return True
        
    return False


async def check_documents_consistency(procurement_id: str) -> Dict[str, Any]:
    """
    Проверяет согласованность данных между документами в рамках одной закупки.

    Функция извлекает исходные данные по идентификатору закупки, исключает из анализа
    документ типа "Проект контракта", подготавливает оставшиеся документы и передаёт их
    в модуль искусственного интеллекта для выявления несоответствий (например, расхождения
    в суммах, сроках, наименованиях участников и т.д.). Возвращает структурированный отчёт
    о найденных проблемах или подтверждение согласованности.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.

    Returns:
        Dict[str, Any]: Словарь с результатом проверки, содержащий:
            - "status": общий статус ("ok", "warning", "error"),
            - "issues": список выявленных несоответствий (может быть пустым),
            - "conclusion": текстовое заключение по результатам анализа.
    """
    try:
        raw_data = get_raw_data_by_procurement_id(procurement_id)
        if not raw_data:
            return {
                "status": "ok",
                "issues": [],
                "conclusion": "Нет данных для проверки согласованности."
            }

        # Подготавливаем данные для LLM (исключая Проект контракта)
        documents_data = prepare_documents_data_for_consistency_check(raw_data)
        
        if not documents_data:
            return {
                "status": "ok", 
                "issues": [],
                "conclusion": "Недостаточно данных для проверки согласованности (после исключения Проекта контракта)."
            }

        # ВЫЗЫВАЕМ LLM ДЛЯ ПРОВЕРКИ СОГЛАСОВАННОСТИ
        consistency_result = await check_completeness_with_ai(procurement_id, documents_data)
        
        # Преобразуем результат в совместимый формат
        return convert_consistency_result(consistency_result)

    except Exception as e:
        logger.error(f"Ошибка при проверке согласованности: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "issues": [],
            "conclusion": f"Ошибка проверки согласованности: {str(e)}"
        }


def prepare_documents_data_for_final_check(document_analysis_results: Dict[str, Any]) -> str:
    """Подготовка данных из всех проанализированных документов для итоговой проверки."""
    # Пример: сбор raw_data из каждого документа
    docs_data_for_prompt = []
    for doc_name, analysis in document_analysis_results.items():
        if analysis.get("status") == "success":
            doc_info = {
                "filename": doc_name,
                "document_type": analysis.get("document_type", "Неизвестно"),
                "analysis": analysis.get("analysis", {})
            }
            docs_data_for_prompt.append(doc_info)
    return json.dumps(docs_data_for_prompt, ensure_ascii=False, indent=2)


def prepare_requirements_description(document_analysis_results: Dict[str, Any]) -> str:
    """Подготовка описания требований из извещения или документации."""
    # Пример: извлечение attached_documents_list из "Извещения" или "Документации"
    requirements_description = []
    for analysis in document_analysis_results.values():
        if analysis.get("document_type") in ["Извещение", "Документация", "Извещение о заключении контракта"]:
             req_list = analysis.get("analysis", {}).get("raw_data", {}).get("attached_documents_list", [])
             if req_list:
                 requirements_description = req_list
                 break
    return json.dumps(requirements_description, ensure_ascii=False, indent=2)


def create_fallback_final_evaluation() -> Dict[str, Any]:
    """Создаёт резервный результат итоговой проверки при сбое."""
    return {
        "overall_summary": "Целостная проверка не выполнена из-за технической ошибки.",
        "overall_status": "deny",
        "readability": {
            "summary": "Проверка читаемости не выполнена.",
            "status": "deny",
            "documents": []
        },
        "type_compliance": {
            "summary": "Проверка типа и комплекта не выполнена.",
            "status": "deny",
            "documents": []
        },
        "completeness": {
            "summary": "Проверка полноты не выполнена.",
            "status": "deny",
            "description": "Проверка не выполнена из-за ошибки."
        }
    }


def create_unprocessed_document_analysis(filename: str, error: str) -> Dict[str, Any]:
    """
    Создаёт заглушку анализа для документа, который не удалось обработать.
    Используется для передачи информации в финальную модель.
    """
    return {
        "status": "error",
        "document_name": filename,
        "document_type": "Дополнительные материалы",
        "analysis": {
            "type_compliance": {
                "status": "не соответствует",
                "issues": [f"Не удалось обработать документ: {error}"],
                "expected_type": "Дополнительные материалы",
                "actual_type": "Дополнительные материалы",
                "confidence": 0.0
            },
            "readability": {
                "status": "неудовлетворительно",
                "issues": [f"Ошибка обработки: {error}"]
            },
            "raw_data": {
                "dates": [],
                "amounts": [],
                "legal_entities": [],
                "contract_number": "",
                "law_references": [],
                "attached_documents_list": []
            },
            "conclusion": f"Документ '{filename}' не был проанализирован из-за ошибки: {error}"
        }
    }


async def check_completeness_with_ai(procurement_id: str) -> Dict[str, Any]:
    """
    Выполняет объединённую проверку читаемости, типа/комплекта и полноты документов
    с помощью ИИ, используя СЫРЫЕ ДАННЫЕ ИЗ БД.
    """
    try:
        logger.info(f"Запуск объединённой проверки (читаемость, тип/комплект, полнота) с ИИ для закупки {procurement_id}")

        # === 1. Получаем ВСЕ сырые данные из БД ===
        raw_data_from_db = get_raw_data_by_procurement_id(procurement_id)
        if not raw_data_from_db:
            logger.warning(f"Нет данных в БД для procurement_id={procurement_id}")
            return create_fallback_final_evaluation()

        # === 2. Подготавливаем данные для промпта ===
        # Форматируем как читаемый текст: тип документа + его raw_data
        documents_data_lines = []
        for doc_type, analysis in raw_data_from_db.items():
            if isinstance(analysis, dict):
                # Убираем служебные поля, если они есть (например, summary_report)
                clean_analysis = {
                    k: v for k, v in analysis.items()
                    if k not in ["summary_report", "id", "created_at", "updated_at"]
                }
                documents_data_lines.append(f"=== {doc_type} ===\n{json.dumps(clean_analysis, ensure_ascii=False, indent=2)}")
            else:
                # Если значение не словарь (например, строка JSON), попробуем распарсить
                try:
                    parsed = json.loads(analysis)
                    documents_data_lines.append(f"=== {doc_type} ===\n{json.dumps(parsed, ensure_ascii=False, indent=2)}")
                except (TypeError, json.JSONDecodeError):
                    documents_data_lines.append(f"=== {doc_type} ===\n{str(analysis)}")

        documents_data_str = "\n\n".join(documents_data_lines)

        # === 3. Подготавливаем требования (из attached_documents_list в Извещении и т.п.) ===
        requirements_description = []
        for doc_type, analysis in raw_data_from_db.items():
            if doc_type in ["Извещение", "Документация", "Извещение о закупке"]:
                try:
                    raw_data = analysis.get("raw_data", {}) if isinstance(analysis, dict) else json.loads(analysis).get("raw_data", {})
                    attached = raw_data.get("attached_documents_list", [])
                    if attached:
                        requirements_description = attached
                        break
                except Exception:
                    continue

        requirements_description_str = json.dumps(requirements_description, ensure_ascii=False, indent=2)

        # === 4. Формируем USER-промпт ===
        user_prompt = USER_FINAL_EVALUATION_PROMPT.format(
            documents_data=documents_data_str,
            requirements_description=requirements_description_str
        )

        # === 5. Вызываем модель ===
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": SYSTEM_FINAL_EVALUATION_PROMPT},
                        {"role": "user", "content": user_prompt}
                    ],
                    extra_body={"guided_json": FINAL_EVALUATION_SCHEMA},
                )
            ),
            timeout=300.0
        )

        raw_response = response.choices[0].message.content.strip()
        logger.debug(f"Сырой ответ модели (первые 500 символов): {raw_response[:500]}...")

        # === 6. Парсим и валидируем ===
        result = json.loads(raw_response)

        # Минимальная валидация структуры
        required_keys = ["overall_summary", "overall_status", "readability", "type_compliance", "completeness"]
        if not all(k in result for k in required_keys):
            raise ValueError("Неполная структура ответа")

        logger.info(f"Объединённая проверка завершена успешно для {procurement_id}. Статус: {result['overall_status']}")
        return result

    except asyncio.TimeoutError:
        logger.error(f"Таймаут при выполнении объединённой проверки для {procurement_id}")
        return create_fallback_final_evaluation()
    except Exception as e:
        logger.error(f"Ошибка в check_completeness_with_ai: {str(e)}", exc_info=True)
        return create_fallback_final_evaluation()
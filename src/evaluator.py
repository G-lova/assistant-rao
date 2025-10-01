import os
import logging
import json
import re
import asyncio

from typing import List, Dict, Any, Tuple
from openai import OpenAI
from dotenv import load_dotenv

from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.prompts import (RESPONSE_JSON_SCHEMA,
                         SYSTEM_PROMPT,
                         TYPE_DETECTION_PROMPT,
                         COMPLETENESS_CHECK_PROMPT,
                         COMPLETENESS_CHECK_SCHEMA)
from configs.working_with_db import save_raw_data, save_clean_conclusion, get_raw_data_by_procurement_id
from configs.utils import check_procurement_completeness


load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


client = OpenAI(
    base_url=os.getenv("M_MODEL_API_URL"),
    api_key=os.getenv("M_MODEL_API_KEY")
)
model_name = os.getenv("M_MODEL_NAME")


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
    Асинхронно анализирует фрагмент документа с использованием языковой модели и возвращает структурированный результат.

    Функция отправляет текстовый контент в LLM с системным промптом, соответствующим ожидаемому типу документа,
    ограничивает размер входных данных и устанавливает таймаут на выполнение. После получения ответа
    выполняет надёжный парсинг JSON, включая извлечение через регулярные выражения при необходимости.
    Поскольку тип документа уже известен (передан как аргумент), функция устанавливает его как «фактический»
    и помечает соответствие как успешное (кроме случая «Дополнительные материалы»).

    Args:
        content (str): Текстовое содержимое документа или его фрагмента.
        document_name (str): Имя файла или идентификатор фрагмента для логирования.
        document_type (str): Ожидаемый и фактический тип документа (например, «Техническое задание»).
        law_type (str): Тип законодательства (например, «44-ФЗ»), используется в промпте для контекста.
        procurement_method (str): Способ закупки (например, «Конкурс»), также передаётся в промпт.

    Raises:
        ValueError: Если произошёл таймаут при вызове модели.

    Returns:
        Dict[str, Any]: Словарь с результатом анализа, содержащий:
            - status (str): «success» или «error».
            - document_name (str): Имя документа.
            - document_type (str): Тип документа.
            - analysis (dict): Структурированный результат, включающий:
                * type_compliance — соответствие типа (всегда «соответствует», если тип не «Дополнительные материалы»),
                * readability — оценка читаемости,
                * raw_data — извлечённые сущности (даты, суммы, юрлица и др.),
                * conclusion — текстовое заключение.
            При ошибке возвращается объект с диагностикой.
    """
    try:
        logger.info(f"Анализ чанка '{document_name}' с типом '{document_type}'")
        logger.info(f"Длина контента: {len(content)} символов")

        # Ограничиваем размер контента для предотвращения ошибок
        if len(content) > 20000:
            content = content[:20000] + "... [контент обрезан]"
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
                        max_tokens=4000  # Ограничиваем выходные токены
                    )
                ),
                timeout=60.0  # 60 секунд таймаут
            )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при анализе {document_name}")
            raise ValueError("Таймаут при анализе документа")

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


async def check_documents_consistency(procurement_id: str) -> Dict[str, Any]:
    """
    Проверяет согласованность ключевых полей между различными документами одной закупки.

    Функция извлекает сырые данные всех документов по указанному идентификатору закупки,
    собирает значения критических полей (например, номер контракта, НМЦК, заказчик) и проверяет,
    совпадают ли они во всех документах. При расхождениях формирует отчёт с указанием
    документов-источников и степени серьёзности проблемы.

    Args:
        procurement_id (str): Уникальный идентификатор закупки, для которой проводится проверка.

    Returns:
        Dict[str, Any]: Словарь с результатами проверки, содержащий:
            - status (str): Общий статус — "ok" (всё согласовано) или "error" (обнаружены расхождения).
            - issues (List[dict]): Список выявленных проблем, каждый элемент включает:
                - field (str): Название поля с расхождением.
                - problem (str): Тип проблемы (например, "расхождение значений").
                - details (str): Подробное описание несоответствий и в каких документах обнаружено.
                - severity (str): Уровень важности — "high" (критичные поля) или "medium".
            - conclusion (str): Человекочитаемое заключение, объединяющее все найденные проблемы.
            В случае ошибки возвращается статус "error" и сообщение об исключении.
    """
    try:
        raw_data = get_raw_data_by_procurement_id(procurement_id)
        if not raw_data:
            return {
                "status": "ok",
                "issues": [],
                "conclusion": "Нет данных для проверки согласованности."
            }

        consistency_issues = []
        field_values = {}

        # Собираем значения полей из всех документов
        for doc_type, doc_data in raw_data.items():
            if not isinstance(doc_data, dict):
                continue
            
            # Извлекаем данные из raw_data
            doc_raw_data = doc_data.get("raw_data", {})
            
            # Проверяем ключевые поля
            key_fields = ["contract_number", "customer_name", "procurement_object", "initial_price"]
            
            for field in key_fields:
                value = None
                
                # Ищем поле в различных местах структуры
                if field in doc_raw_data:
                    value = doc_raw_data[field]
                elif field == "contract_number" and "contract_number" in doc_raw_data:
                    value = doc_raw_data["contract_number"]
                elif field == "customer_name" and "legal_entities" in doc_raw_data:
                    for entity in doc_raw_data["legal_entities"]:
                        if entity.get("role") == "заказчик":
                            value = entity.get("name")
                            break
                elif field == "initial_price" and "amounts" in doc_raw_data:
                    for amount in doc_raw_data["amounts"]:
                        if "нмцк" in amount.get("field", "").lower():
                            value = amount.get("value")
                            break
                
                if value:
                    field_values.setdefault(field, []).append({
                        "value": str(value).strip(),
                        "document": doc_type,
                        "source": doc_raw_data
                    })

        # Проверяем расхождения
        for field, values in field_values.items():
            unique_values = set(item["value"] for item in values)
            if len(unique_values) > 1:
                issue_details = []
                for unique_val in unique_values:
                    docs_with_value = [item["document"] for item in values if item["value"] == unique_val]
                    issue_details.append(f'"{unique_val}" в документах: {", ".join(docs_with_value)}')
                
                consistency_issues.append({
                    "field": field,
                    "problem": "расхождение значений",
                    "details": "; ".join(issue_details),
                    "severity": "high" if field in ["contract_number", "initial_price"] else "medium"
                })

        # Формируем заключение
        if not consistency_issues:
            conclusion = "Все ключевые поля согласованы между документами."
            status = "ok"
        else:
            conclusion_lines = ["Обнаружены расхождения между документами:"]
            for issue in consistency_issues:
                conclusion_lines.append(f"- {issue['field']}: {issue['details']}")
            conclusion = "\n".join(conclusion_lines)
            status = "error"

        return {
            "status": status,
            "issues": consistency_issues,
            "conclusion": conclusion
        }

    except Exception as e:
        logger.error(f"Ошибка при проверке согласованности: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "issues": [],
            "conclusion": f"Ошибка проверки согласованности: {str(e)}"
        }


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
            timeout=30.0
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


async def check_completeness_with_ai(
    procurement_id: str,
    required_documents: List[str],
    provided_documents: List[str]
) -> Dict[str, Any]:
    """
    Проверяет комплектность документов с помощью ИИ, учитывая смысловое соответствие.

    Args:
        procurement_id (str): ID закупки
        required_documents (List[str]): Список обязательных документов
        provided_documents (List[str]): Список загруженных документов

    Returns:
        Dict[str, Any]: Результат проверки комплектности
    """
    try:
        logger.info(f"Проверка комплектности с ИИ для закупки {procurement_id}")
        logger.info(f"Обязательные документы: {required_documents}")
        logger.info(f"Загруженные документы: {provided_documents}")

        if not required_documents:
            return {
                "completeness_status": "полный",
                "missing_documents": [],
                "document_mapping": {},
                "reasoning": "Список обязательных документов не указан"
            }

        # Формируем промпт
        prompt = COMPLETENESS_CHECK_PROMPT.format(
            required_docs=json.dumps(required_documents, ensure_ascii=False, indent=2),
            provided_docs=json.dumps(provided_documents, ensure_ascii=False, indent=2)
        )

        # Вызываем модель
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": "Ты — эксперт по проверке комплектности документов закупки."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=2000,
                    temperature=0.1,
                    extra_body={"guided_json": COMPLETENESS_CHECK_SCHEMA}
                )
            ),
            timeout=45.0
        )

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Ответ модели для проверки комплектности: {raw_response}")

        # Парсим JSON
        try:
            result = json.loads(raw_response)
            
            # Валидация результата
            if not all(key in result for key in ["completeness_status", "missing_documents", "document_mapping", "reasoning"]):
                raise ValueError("Неполный ответ от модели")
                
            logger.info(f"Проверка комплектности завершена: статус - {result['completeness_status']}")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON при проверке комплектности: {e}")
            logger.error(f"Сырой ответ: {raw_response}")
            return create_fallback_completeness_result(required_documents, provided_documents)

    except asyncio.TimeoutError:
        logger.error(f"Таймаут при проверке комплектности для {procurement_id}")
        return create_fallback_completeness_result(required_documents, provided_documents)
    except Exception as e:
        logger.error(f"Ошибка при проверке комплектности: {str(e)}", exc_info=True)
        return create_fallback_completeness_result(required_documents, provided_documents)


def create_fallback_completeness_result(required_docs: List[str], provided_docs: List[str]) -> Dict[str, Any]:
    """
    Создает резервный результат проверки комплектности при ошибке ИИ.
    
    Args:
        required_docs (List[str]): Обязательные документы
        provided_docs (List[str]): Загруженные документы
        
    Returns:
        Dict[str, Any]: Резервный результат
    """
    # Простое сравнение без семантического анализа
    missing_docs = []
    document_mapping = {}
    
    # Базовое сопоставление по ключевым словам
    for req_doc in required_docs:
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
        "reasoning": "Проверка выполнена базовым методом (без семантического анализа)"
    }


def is_document_match(required_doc: str, provided_doc: str) -> bool:
    """
    Проверяет соответствие документов по ключевым словам.
    
    Args:
        required_doc (str): Обязательный документ
        provided_doc (str): Загруженный документ
        
    Returns:
        bool: True если документы соответствуют
    """
    required_lower = required_doc.lower()
    provided_lower = provided_doc.lower()
    
    # Группы эквивалентных документов
    contract_group = ["проект контракта", "проект договора", "контракт", "договор"]
    price_group = ["обоснование н(м)цк", "обоснование цены", "расчет нмцк", "смета"]
    tech_group = ["техническое задание", "тз", "описание объекта закупки"]
    notice_group = ["извещение", "уведомление о закупке"]
    requirements_group = ["требования к содержанию заявки", "инструкция для участников"]
    
    # Проверка принадлежности к одной группе
    groups = [contract_group, price_group, tech_group, notice_group, requirements_group]
    
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
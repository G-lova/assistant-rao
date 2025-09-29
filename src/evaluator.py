import os
import logging
import json
import re
import asyncio

from typing import List, Dict, Any, Tuple
from openai import OpenAI
from dotenv import load_dotenv

from configs.utils import normalize_document_type, read_file, check_procurement_completeness
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.prompts import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPT, TYPE_DETECTION_PROMPT
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


def split_large_text(text: str, max_chunk_size: int = 30000) -> List[str]:
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
    Анализирует документ с двухэтапным подходом.
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
    
    # ЭТАП 1: Определение типа документа по первым 500 символам
    first_500_chars = chunks[0][:500]
    detected_type = await detect_document_type(first_500_chars, document_name)
    
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
    Анализирует один чанк документа с УЖЕ ИЗВЕСТНЫМ типом.
    """
    try:
        logger.info(f"Анализ чанка '{document_name}' с типом '{document_type}'")

        # Формируем промпт с подстановкой УЖЕ ИЗВЕСТНОГО типа
        prompt = SYSTEM_PROMPT.format(document_type=document_type)

        # Вызов модели с guided_json
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Проанализируй следующий документ типа '{document_type}':\n\n{content}"}
            ],
            extra_body={"guided_json": RESPONSE_JSON_SCHEMA}
        )

        raw_response = response.choices[0].message.content.strip()

        # Парсим JSON
        try:
            result = json.loads(raw_response)
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON: {e}")
            # Попробуем извлечь JSON вручную
            start = raw_response.find("{")
            end = raw_response.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    cleaned_text = raw_response[start:end]
                    cleaned_text = re.sub(r"/\*.*?\*/", "", cleaned_text, flags=re.DOTALL)
                    cleaned_text = re.sub(r"//.*$", "", cleaned_text, flags=re.MULTILINE)
                    result = json.loads(cleaned_text)
                except Exception as je:
                    raise ValueError(f"Не удалось восстановить JSON: {je}")
            else:
                raise ValueError("Не найден JSON в ответе модели")

        # Устанавливаем известный тип
        type_compliance = result.get("type_compliance", {})
        type_compliance["actual_type"] = document_type
        type_compliance["expected_type"] = document_type
        
        # Проверяем соответствие содержания типу
        if document_type != "Дополнительные материалы":
            # Проверяем, есть ли признаки несоответствия в контенте
            content_lower = content.lower()
            expected_keywords = {
                "Проект контракта": ["договор", "контракт", "стороны", "заказчик", "поставщик"],
                "Извещение": ["извещение", "закупка", "размещение заказа"],
                "Обоснование н(м)цк": ["обоснование", "нмцк", "расчет", "цена"],
                "Техническое задание": ["техническое задание", "требования", "характеристики"],
                "Описание объекта закупки": ["описание объекта", "предмет закупки"],
                "Требования к содержанию заявки на конкурс": ["требования к заявке", "состав заявки"]
            }
            
            keywords = expected_keywords.get(document_type, [])
            has_keywords = any(keyword in content_lower for keyword in keywords)
            
            if has_keywords:
                type_compliance["status"] = "соответствует"
                type_compliance["issues"] = []
                type_compliance["confidence"] = 0.9
            else:
                type_compliance["status"] = "частично соответствует"
                type_compliance["issues"] = ["В содержании недостаточно признаков ожидаемого типа"]
                type_compliance["confidence"] = 0.6
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
                    "status": "неудовлетворительно",
                    "issues": ["Не оценено"]
                }),
                "raw_data": result.get("raw_data", {}),
                "conclusion": result.get("conclusion", f"Документ типа '{document_type}' проанализирован.")
            }
        }

    except Exception as e:
        logger.error(f"Ошибка при анализе {document_name}: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "document_name": document_name,
            "document_type": document_type,
            "analysis": {
                "type_compliance": {
                    "status": "не соответствует",
                    "issues": [f"Ошибка анализа: {str(e)}"],
                    "expected_type": document_type,
                    "actual_type": document_type,
                    "confidence": 0.0
                },
                "readability": {
                    "status": "неудовлетворительно",
                    "issues": ["Ошибка обработки"]
                },
                "raw_data": {},
                "conclusion": f"Анализ не выполнен: {str(e)}"
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
    Нормализует строковое название типа документа к единому каноническому виду.

    Функция преобразует входную строку, очищает её и сравнивает с эталонными типами документов.
    Сначала проверяет точное совпадение (с учётом регистра), затем — частичное вхождение ключевых слов.
    Возвращает стандартизированное имя документа согласно внутреннему маппингу. 
    Используется для унификации типов, определённых моделью, перед сохранением в БД.

    Args:
        doc_type (str): Исходное название типа документа (например, извлечённое LLM).

    Returns:
        str: Нормализованное название типа документа. Возможные значения:
            - "Требования к содержанию заявки на конкурс"
            - "Техническое задание"
            - "Извещение"
            - "Проект контракта"
             Если соответствие не найдено — возвращается "Дополнительные материалы".
    """
    if not doc_type or not isinstance(doc_type, str):
        return "Дополнительные материалы"

    doc_type_clean = doc_type.strip().lower()

    for key in DOCUMENT_TYPE_MAPPING:
        if doc_type_clean == key.lower().strip():
            return key

    for key in DOCUMENT_TYPE_MAPPING:
        key_lower = key.lower()
        if doc_type_clean in key_lower or key_lower in doc_type_clean:
            if "требования к содержанию заявки на конкурс" in key_lower:
                return "Требования к содержению заявки на конкурс"
            if "техническое задание" in key_lower:
                return "Техническое задание"
            if "извещение" in key_lower:
                return "Извещение"
            if "проект контракта" in key_lower:
                return "Проект контракта"

    return "Дополнительные материалы"


async def detect_document_type(first_500_chars: str, document_name: str) -> str:
    """
    Определяет тип документа по первым 500 символам.
    """
    try:
        logger.info(f"Определение типа документа: {document_name}")
        
        # Формируем промпт с доступными типами
        allowed_types_str = "\n".join([f"- {t}" for t in ALLOWED_DOC_TYPES])
        prompt = TYPE_DETECTION_PROMPT.format(allowed_types=allowed_types_str)
        
        # Вызываем модель для определения типа с таймаутом
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"Определи тип документа:\n\n{first_500_chars}"}
                    ],
                    max_tokens=100,
                    temperature=0.1
                )
            ),
            timeout=30.0  # 30 секунд таймаут
        )
        
        detected_type = response.choices[0].message.content.strip()
        logger.info(f"Модель определила тип: '{detected_type}' для документа '{document_name}'")
        
        # Нормализуем результат
        normalized_type = normalize_document_type(detected_type)
        logger.info(f"Нормализованный тип: '{normalized_type}'")
        
        return normalized_type
        
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при определении типа документа {document_name}")
        return "Дополнительные материалы"
    except Exception as e:
        logger.error(f"Ошибка при определении типа документа {document_name}: {str(e)}")
        return "Дополнительные материалы"
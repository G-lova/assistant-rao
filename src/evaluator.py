import os
import logging
import json
import re
import asyncio

from typing import List, Dict, Any, Tuple
from openai import OpenAI
from dotenv import load_dotenv

from configs.utils import read_file
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.prompts import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPT
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


def split_large_text(text: str, max_chunk_size: int = 40000) -> List[str]:
    """
    Разбивает большой текст на фрагменты заданного максимального размера с сохранением смысловой целостности.

    Сначала пытается разделить текст по абзацам, чтобы не разрывать логические блоки.
    Если после разбиения по абзацам получаются слишком крупные фрагменты — переключается
    на разбиение по предложениям. Гарантирует, что каждый фрагмент не превышает `max_chunk_size`
    и при этом максимально сохраняет контекст для последующей обработки (например, LLM).

    Args:
        text (str): Исходный текст, который необходимо разбить на части.
        max_chunk_size (int, optional): Максимально допустимый размер одного фрагмента в символах.
            По умолчанию — 40000 (подходит для моделей с большим контекстом).

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
    Асинхронно анализирует документ, разбитый на фрагменты, с объединением результатов.

    Функция предназначена для обработки больших документов, которые нельзя передать целиком в LLM.
    Анализ начинается с первого фрагмента — по нему определяется тип документа и проводится первичная оценка.
    Последующие фрагменты анализируются отдельно для извлечения дополнительных данных, после чего все результаты
    объединяются в единый вывод с помощью функции `merge_analysis_results`.

    Args:
        chunks (List[str]): Список текстовых фрагментов, на которые был разделён документ.
        document_name (str): Имя исходного документа (для логирования и отчётов).
        document_type (str): Ожидаемый тип документа (например, 'contract', 'act'), используется в промпте.
        law_type (str): Тип законодательства (например, '44-ФЗ'), влияет на контекст анализа.
        procurement_method (str): Способ закупки (например, 'конкурс'), учитывается моделью при оценке.

    Returns:
        Dict[str, Any]: Единый словарь с результатами анализа, содержащий:
            - status (str): 'success' или 'error'.
            - analysis (dict): Объединённые данные анализа:
                - type_compliance: Соответствие типа документа.
                - readability: Оценка читаемости.
                - raw_data: Извлечённые структурированные данные из всех блоков.
                - conclusion: Итоговое заключение.
            В случае пустого документа или ошибки возвращается соответствующий шаблон с диагностикой.
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
    
    # Анализируем первый блок для определения типа документа
    first_chunk_result = await analyze_single_document(
        content=chunks[0],
        document_name=document_name,
        document_type=document_type,
        law_type=law_type,
        procurement_method=procurement_method
    )
    
    if first_chunk_result["status"] != "success":
        return first_chunk_result
    
    # Если документ большой, анализируем остальные блоки для извлечения данных
    if len(chunks) > 1:
        additional_results = []
        for i, chunk in enumerate(chunks[1:], 2):
            try:
                result = await analyze_single_document(
                    content=chunk,
                    document_name=f"{document_name} (блок {i})",
                    document_type=document_type,
                    law_type=law_type,
                    procurement_method=procurement_method
                )
                if result["status"] == "success":
                    additional_results.append(result)
            except Exception as e:
                logger.warning(f"Ошибка анализа блока {i}: {e}")
        
        # Объединяем результаты
        if additional_results:
            first_chunk_result = merge_analysis_results([first_chunk_result] + additional_results)
    
    return first_chunk_result


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
            f"Документ проанализирован по частям ({len(results)} блоков). " +
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
    Асинхронно анализирует содержимое одного документа с помощью языковой модели.

    Функция отправляет текст документа в LLM с системным промптом, ожидая структурированный JSON-ответ.
    Выполняет парсинг результата, нормализует определённый моделью тип документа через внутреннее маппинг-правило,
    проверяет соответствие и формирует единый результат с заключением. При ошибках возвращает детализированный отчёт.

    Args:
        content (str): Полный текст документа для анализа (например, извлечённый из PDF).
        document_name (str): Имя файла документа — используется для логирования.
        document_type (str): Ожидаемый тип документа (например, 'Техническое задание'), передаётся в промпт.
        law_type (str): Тип законодательства (например, '44-ФЗ'), влияет на контекст анализа.
        procurement_method (str): Способ закупки (например, 'Конкурс', 'Аукцион') — может использоваться в будущем.

    Raises:
        ValueError: Если не удаётся извлечь или распарсить JSON из ответа модели.
        ValueError: Если JSON не найден в ответе после очистки от комментариев.

    Returns:
        Dict[str, Any]: Словарь с результатом анализа, содержащий:
            - status (str): 'success' или 'error'.
            - document_name (str): Имя обработанного документа.
            - document_type (str): Переданный тип документа.
            - analysis (dict): Данные анализа:
                - type_compliance: Проверка соответствия типа с нормализованными значениями.
                - readability: Оценка читаемости документа.
                - raw_data: Извлечённые структурированные данные.
                - conclusion: Итоговое заключение с учётом результата нормализации.
    """
    try:
        logger.info(f"Анализ документа: {document_name} | Тип: {document_type}")

        # Формируем промпт с подстановкой
        prompt = SYSTEM_PROMPT.format(document_type=document_type)

        # Вызов модели с guided_json
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Проанализируй следующий документ:\n\n{content}"}
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

        # Нормализация типа документа
        type_compliance = result.get("type_compliance", {})
        actual_raw = type_compliance.get("actual_type", "").strip()
        expected_raw = type_compliance.get("expected_type", document_type).strip()

        actual_normalized = normalize_document_type(actual_raw)
        expected_normalized = normalize_document_type(expected_raw)

        type_compliance["actual_type"] = actual_normalized
        type_compliance["expected_type"] = expected_normalized

        if actual_normalized == "Дополнительные материалы":
            type_compliance["status"] = "не соответствует"
            issues = type_compliance.get("issues", [])
            issues.append("Не удалось определить тип документа.")
            type_compliance["issues"] = issues
            conclusion_suffix = "Тип документа не распознан."
        else:
            type_compliance["status"] = "соответствует"
            type_compliance["issues"] = []
            conclusion_suffix = f"Тип документа подтверждён: {actual_normalized}."

        old_conclusion = result.get("conclusion", "")
        result["conclusion"] = f"{conclusion_suffix} {old_conclusion}".strip()

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
                "conclusion": result["conclusion"]
            }
        }

    except Exception as e:
        logger.error(f"Критическая ошибка при анализе {document_name}: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "document_name": document_name,
            "document_type": document_type,
            "analysis": {
                "type_compliance": {
                    "status": "не соответствует",
                    "issues": [f"Ошибка анализа: {str(e)}"],
                    "expected_type": document_type,
                    "actual_type": "неизвестно"
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
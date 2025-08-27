import os
import logging
import tempfile
import json
import re
import asyncio

from typing import List, Dict, Any
from openai import OpenAI
from dotenv import load_dotenv

from configs.utils import read_file
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from search_engine.service import search_similar_documents as sync_search_similar
from src.prompts import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPT


load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


client = OpenAI(
    base_url=os.getenv("M_MODEL_API_URL"),
    api_key=os.getenv("M_MODEL_API_KEY")
)
model_name = os.getenv("M_MODEL_NAME")


def extract_json_safely(text: str) -> dict:
    """
    Извлекает и парсит JSON из строки, содержащей дополнительный текст или форматирование.

    Функция пытается найти JSON-объект в "грязной" строке, например, в ответе языковой модели,
    который может содержать Markdown-разметку (````json```), лишние запятые, одинарные кавычки
    и другие нестандартные элементы. Очищает строку и преобразует её в валидный словарь Python.

    Args:
        text (str): Входная строка, содержащая JSON (возможно, вместе с дополнительным текстом).

    Raises:
        ValueError: Если в строке не удаётся найти корректный JSON-объект.
        json.JSONDecodeError: Если очищенная строка не является валидным JSON.

    Returns:
        dict: Распарсенный JSON как словарь Python.
    """
    try:
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```\s*', '', text)
        first_brace = text.find('{')
        last_brace = text.rfind('}')

        if first_brace == -1 or last_brace == -1:
            raise ValueError("No JSON object found")
        
        text = text[first_brace:last_brace + 1]
        text = re.sub(r',\s*}', '}', text)
        text = re.sub(r',\s*\]', ']', text)
        text = re.sub(r"'([^']+)'(?=\s*:)", r'"\1"', text)
        text = re.sub(r":\s*'([^']*)'", r': "\1"', text)
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON Decode Error: {e} | Raw: {repr(text)}")
        raise


async def analyze_single_document(content: str, doc_name: str, doc_type: str, law_type: str, procurement_method: str) -> Dict[str, Any]:
    """
    Анализирует отдельный документ на соответствие типу, структуре и читаемости.

    Функция использует LLM для оценки:
    - соответствия содержания документа ожидаемому типу (например, спецификация, смета);
    - читаемости текста (наличие шрифтов, язык, повреждённые фрагменты);
    - соответствия требованиям законодательства и способу закупки.

    Перед анализом выполняется поиск похожих документов в векторной базе для контекстного улучшения.
    Ответ модели ожидается в строгом JSON-формате, который проходит валидацию и очистку.

    Args:
        content (str): Текстовое содержимое документа (полное или частичное).
        doc_name (str): Оригинальное имя файла, используется для логирования и анализа.
        doc_type (str): Ожидаемый тип документа (например, "Спецификация", "Смета").
        law_type (str): Тип законодательства (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ закупки (например, "Конкурс", "Котировка").

    Returns:
        Dict[str, Any]: Словарь с результатами анализа, содержащий:
            - type_compliance: статус соответствия, выявленные проблемы и рекомендации;
            - readability: оценка читаемости, язык текста, проблемные фрагменты.
            В случае ошибки возвращается шаблон с ошибкой и статусом "не соответствует".
    """
    try:
        # Поиск похожих документов
        search_text = content[:1000]
        try:
            loop = asyncio.get_event_loop()
            similar_docs = await loop.run_in_executor(None, sync_search_similar, search_text, law_type)
        except Exception as e:
            logger.warning(f"Поиск похожих документов не удался: {e}")
            similar_docs = []

        similar_text = "\n".join([
            f"- {doc['name']} (схожесть: {doc['score']:.3f}): {doc['text_snippet']}"
            for doc in similar_docs
        ])

        # Логируем контекст похожих документов
        logger.info(f"Похожие документы для {doc_name}:\n{similar_text if similar_text else 'Нет'}")

        prompt = f"""
Тип закупки: {law_type}
Способ закупки: {procurement_method}
Имя файла: {doc_name}
Ожидаемый тип: {doc_type}
Контекст похожих документов:
{similar_text if similar_text else 'Нет похожих документов'}

Содержимое (первые 4000 символов):
{content[:4000]}
"""

        # Логируем промпт
        logger.info(f"Промпт для модели (первые 800 символов):\n{prompt[:800]}")

        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(similar_docs=similar_text)},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=1024,
            extra_body={"guided_json": RESPONSE_JSON_SCHEMA}
        )

        raw_content = completion.choices[0].message.content.strip()
        
        # Логируем сырой ответ модели
        logger.info(f"Сырой ответ модели для {doc_name}:\n{raw_content}")

        result = extract_json_safely(raw_content)

        # Логируем распарсенный JSON
        logger.info(f"Распарсенный результат для {doc_name}:\n{json.dumps(result, ensure_ascii=False, indent=2)}")

        # Валидация...
        if not isinstance(result, dict):
            raise ValueError("Response is not a JSON object")
        if "type_compliance" not in result or "readability" not in result:
            raise ValueError("Missing required fields")

        status = result["type_compliance"].get("status", "не соответствует")
        if status not in ["соответствует", "не соответствует"]:
            result["type_compliance"]["status"] = "не соответствует"

        for field in ["issues", "recommendations"]:
            if not isinstance(result["type_compliance"].get(field, []), list):
                result["type_compliance"][field] = []

        for field in ["issues", "problematic_fragments"]:
            if not isinstance(result["readability"].get(field, []), list):
                result["readability"][field] = []

        lang = result["readability"].get("language", "неизвестно")
        if lang not in ["русский", "английский", "смешанный"]:
            result["readability"]["language"] = "неизвестно"

        return result

    except Exception as e:
        logger.error(f"Ошибка при анализе документа {doc_name}: {e}", exc_info=True)
        return {
            "type_compliance": {
                "status": "не соответствует",
                "issues": ["Ошибка анализа: не удалось получить ответ от модели"],
                "recommendations": ["Проверьте документ вручную"]
            },
            "readability": {
                "is_readable": False,
                "language": "неизвестно",
                "issues": ["Ошибка обработки"],
                "problematic_fragments": []
            }
        }

async def check_documents(
    file_paths: List[str],
    original_filenames: List[str],
    legislation: str = "44-ФЗ",
    procurement_method: str = "Конкурс",
    expertise_details: str = "Полный комплект документов о закупке"
) -> Dict[str, Any]:
    """
    Проверяет набор документов на соответствие типу, читаемости и требованиям законодательства.

    Функция анализирует каждый загруженный документ, определяет его тип по имени файла,
    извлекает текст и проводит анализ с учётом указанного законодательства (например, 44-ФЗ)
    и способа закупки. Формирует детализированный отчёт по каждому документу и общую оценку.

    Args:
        file_paths (List[str]): Список путей к временным файлам документов на диске.
        original_filenames (List[str]): Список оригинальных имён файлов (для корректного определения типа).
        legislation (str, optional): Номер закона, по которому проводится проверка. Defaults to "44-ФЗ".
        procurement_method (str, optional): Способ проведения закупки (например, "Конкурс", "Аукцион"). Defaults to "Конкурс".
        expertise_details (str, optional): Дополнительные сведения о цели экспертизы. Defaults to "Полный комплект документов о закупке".

    Returns:
        Dict[str, Any]: Словарь с результатами проверки, содержащий:
            - overall_compliance: общий статус соответствия ("соответствует" / "не соответствует");
            - confidence_level: уровень уверенности в проверке;
            - document_analysis: список анализа по каждому документу (включая тип, читаемость, замечания);
            - completeness_check: проверку на полноту комплекта (пока пустая).
            В случае ошибки возвращает словарь с ключом "error".
    """
    if not file_paths:
        return {"error": "Необходимо загрузить хотя бы один документ"}

    document_analysis = []
    for file_path, original_filename in zip(file_paths, original_filenames):
        basename = os.path.basename(original_filename)
        doc_type = "Документ"
        for key in DOCUMENT_TYPE_MAPPING.keys():
            if key.lower() in basename.lower():
                doc_type = key
                break

        try:
            # Читаем файл
            text = read_file(file_path, original_filename=basename)
            analysis = await analyze_single_document(text, basename, doc_type, legislation, procurement_method)
            analysis["document_name"] = basename
            document_analysis.append(analysis)

        except Exception as e:
            logger.error(f"Ошибка при обработке файла {basename}: {str(e)}")
            document_analysis.append({
                "document_name": basename,
                "type_compliance": {
                    "status": "не соответствует",
                    "issues": [f"Ошибка чтения файла: {str(e)}"],
                    "recommendations": []
                },
                "readability": {
                    "is_readable": False,
                    "language": "неизвестно",
                    "issues": ["Ошибка чтения"],
                    "problematic_fragments": []
                }
            })

    overall = "соответствует" if all(
        da.get("type_compliance", {}).get("status") == "соответствует"
        for da in document_analysis
    ) else "не соответствует"

    result = {
        "overall_compliance": overall,
        "confidence_level": "средний",
        "document_analysis": document_analysis,
        "completeness_check": {
            "missing_documents": [],
            "recommendations": []
        }
    }

    return result
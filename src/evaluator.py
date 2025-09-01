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
from search_engine.service import search_similar_documents
from src.prompts import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPT
from configs.working_with_db import save_raw_data, save_clean_conclusion


load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


client = OpenAI(
    base_url=os.getenv("M_MODEL_API_URL"),
    api_key=os.getenv("M_MODEL_API_KEY")
)
model_name = os.getenv("M_MODEL_NAME")


def clean_json_response(text: str) -> str:
    """
    Очищает строку, извлекая из неё корректный JSON-объект.

    Функция находит первый полный JSON-объект (ограниченный фигурными скобками `{}`),
    удаляя всё содержимое до первой открывающей скобки и после последней закрывающей.
    Также удаляются комментарии в стиле C/JavaScript: многострочные `/* ... */` и однострочные `// ...`.

    Args:
        text (str): Входной текст, потенциально содержащий JSON с посторонними символами или комментариями.

    Raises:
        ValueError: Если в тексте не найдена ни одна пара фигурных скобок, определяющих JSON-объект.

    Returns:
        str: Очищенная строка, содержащая только валидный JSON.
    """
    # Удаляем всё до первой { и после последней }
    try:
        start = text.index('{')
        end = text.rindex('}') + 1
        text = text[start:end]
    except ValueError:
        raise ValueError("Не найден JSON в ответе")

    # Убираем комментарии /* ... */ и // ...
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//.*$', '', text, flags=re.MULTILINE)

    return text

def robust_json_parse(text: str) -> dict:
    """
    Надёжно парсит строку в словарь Python, даже при наличии ошибок форматирования.

    Функция пытается распарсить строку как JSON. Если прямой парсинг не удаётся,
    она применяет несколько стратегий очистки и нормализации: удаляет комментарии,
    исправляет кавычки, экранирует переносы строк и заменяет одинарные кавычки на двойные.
    Использует вспомогательную функцию `clean_json_response` для извлечения JSON-объекта.

    Args:
        text (str): Строка, содержащая JSON или неформатированный JSON-подобный текст.

    Raises:
        ValueError: Если после всех попыток очистки и парсинга валидный JSON не был получен.

    Returns:
        dict: Словарь, полученный в результате парсинга JSON.
    """

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Прямой парсинг не удался: {e}")

    # Пробуем почистить
    try:
        cleaned = clean_json_response(text)
        return json.loads(cleaned)
    except Exception:
        pass

    try:
        # Заменяем \n на \\n, если они не экранированы
        text = re.sub(r'(?<!\\)\n', '\\n', text)
        # Заменяем одинарные кавычки на двойные (если модель использует)
        text = text.replace("‘", "'").replace("’", "'").replace("`", "'")
        text = re.sub(r"(\w)'(\w)", r"\1\'\2", text)  # экранируем апострофы
        text = re.sub(r'([^{,:\[])\s*\'', r'\1"', text)  # ' -> " в начале строки
        text = re.sub(r'\'\s*([,}\]\s])', r'"\1', text)  # ' -> " в конце строки

        cleaned = clean_json_response(text)
        return json.loads(cleaned)
    except Exception as e:
        raise ValueError(f"Не удалось распарсить JSON даже после очистки: {e}")


async def analyze_single_document(
    content: str,
    document_name: str,
    document_type: str,
    law_type: str,
    procurement_method: str
) -> Dict[str, Any]:
    """
    Асинхронно анализирует содержимое одного документа с помощью LLM.

    Функция отправляет содержимое документа в языковую модель с системным промптом,
    ожидая структурированный JSON-ответ, соответствующий заданной схеме.
    Выполняет парсинг, валидацию и постобработку результата: проверку соответствия типа,
    восстановление при повреждённом JSON и сохранение результатов в базу данных.
    В случае ошибки возвращает отчёт с диагностикой.

    Args:
        content (str): Текстовое содержимое документа (например, извлечённое из PDF).
        document_name (str): Имя документа для логирования и отчёта.
        document_type (str): Ожидаемый тип документа (например, 'contract', 'act', 'specification').
        law_type (str): Тип законодательства, к которому относится закупка (например, '44-ФЗ', '223-ФЗ').
        procurement_method (str): Способ закупки (например, 'запрос котировок', 'аукцион').

    Raises:
        ValueError: Если не удаётся извлечь или распарсить JSON из ответа модели, и все попытки восстановления провалились.

    Returns:
        Dict[str, Any]: Словарь с результатом анализа, содержащий:
            - status (str): 'success' или 'error'
            - document_name (str): Имя документа
            - document_type (str): Тип документа
            - analysis (dict): Данные анализа, включая:
                - type_compliance: Соответствие типа документа
                - readability: Оценка читаемости
                - raw_data: Извлечённые структурированные данные
                - conclusion: Текстовое заключение
    """
    try:
        logger.info(f"Анализ документа: {document_name} | Тип: {document_type}")

        prompt = SYSTEM_PROMPT.format(document_type=document_type)

        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Проанализируй следующий документ:\n\n{content}"}
                ],
                extra_body={
                    "guided_json": RESPONSE_JSON_SCHEMA
                }
            )
            raw_response = response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Ошибка вызова LLM API для {document_name}: {e}")
            raise

        logger.debug(f"Raw LLM response: {raw_response[:1000]}...")

        # Теперь парсим — ответ должен быть валидным JSON
        try:
            result = json.loads(raw_response)
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON: {e}")
            # Попробуем извлечь JSON вручную
            try:
                start = raw_response.find("{")
                end = raw_response.rfind("}") + 1
                if start == -1 or end == 0:
                    raise ValueError("JSON не найден")
                partial = raw_response[start:end]
                result = json.loads(partial)
            except Exception as e2:
                logger.error(f"Не удалось восстановить JSON: {e2}")
                return {"status": "error", "analysis": { ... }}


        # Проверка обязательных полей
        required_keys = ["type_compliance", "readability", "raw_data", "conclusion"]
        for key in required_keys:
            if key not in result:
                result[key] = {} if key in ["type_compliance", "readability", "raw_data"] else "Заключение недоступно."

        # Проверка соответствия типа
        type_compliance = result["type_compliance"]
        actual_type = type_compliance.get("actual_type", "").strip().lower()
        expected_type = document_type.strip().lower()

        if actual_type and actual_type != expected_type:
            type_compliance["status"] = "не соответствует"
            issues = type_compliance.get("issues", [])
            issues.append(
                f"Документ определён как '{actual_type}', но ожидается тип '{document_type}'."
            )
            type_compliance["issues"] = issues
            result["conclusion"] = (
                f"Документ не соответствует заявленному типу. "
                f"Определён как: '{actual_type}', ожидался: '{document_type}'."
            )

        # Формируем полный анализ
        full_analysis = {
            "raw_data": result["raw_data"],
            "conclusion": result["conclusion"],
            "readability": result["readability"],
            "type_compliance": result["type_compliance"]
        }

        # Сохранение в БД
        try:
            save_raw_data(
                procurement_id=0,
                document_type=document_type,
                full_analysis=full_analysis
            )
            save_clean_conclusion(
                procurement_id=0,
                document_type=document_type,
                conclusion=result["conclusion"]
            )
        except Exception as e:
            logger.error(f"Ошибка сохранения в БД для {document_name}: {e}")

        logger.info(f"Анализ завершён: {document_name}")
        return {
            "status": "success",
            "document_name": document_name,
            "document_type": document_type,
            "analysis": result
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
                    "actual_type": "неизвестно",
                    "confidence": 0.0
                },
                "readability": {
                    "status": "неудовлетворительно",
                    "issues": ["Не удалось распознать содержимое"]
                },
                "raw_data": {},
                "conclusion": f"Анализ не выполнен: {str(e)}"
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
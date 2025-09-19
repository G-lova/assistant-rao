import os
import logging
import json
import re

from typing import List, Dict, Any, Tuple
from openai import OpenAI
from dotenv import load_dotenv

from configs.utils import read_file, get_required_documents
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from search_engine.service import search_similar_documents
from src.prompts import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPT
from configs.working_with_db import save_raw_data, save_clean_conclusion, get_raw_data_by_procurement_id


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

    Args:
        content (str): Текстовое содержимое документа.
        document_name (str): Имя документа для логирования.
        document_type (str): Ожидаемый тип документа (передаётся в промпт).
        law_type (str): Тип законодательства (например, '44-ФЗ').
        procurement_method (str): Способ закупки (например, 'Конкурс').

    Returns:
        Dict[str, Any]: Результат анализа с полями status, document_name, document_type, analysis.
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
        logger.debug(f"Raw LLM response: {raw_response[:1000]}...")

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
                    # Удаляем комментарии
                    cleaned_text = re.sub(r"/\*.*?\*/", "", cleaned_text, flags=re.DOTALL)
                    cleaned_text = re.sub(r"//.*$", "", cleaned_text, flags=re.MULTILINE)
                    result = json.loads(cleaned_text)
                except Exception as je:
                    raise ValueError(f"Не удалось восстановить JSON: {je}")
            else:
                raise ValueError("Не найден JSON в ответе модели")

        # === КЛЮЧЕВАЯ ЛОГИКА: нормализация и проверка типа ===
        type_compliance = result.get("type_compliance", {})
        actual_raw = type_compliance.get("actual_type", "").strip()
        expected_raw = type_compliance.get("expected_type", document_type).strip()

        # Нормализуем оба типа
        actual_normalized = normalize_document_type(actual_raw)
        expected_normalized = normalize_document_type(expected_raw)

        # Обновляем значения в результате
        type_compliance["actual_type"] = actual_normalized
        type_compliance["expected_type"] = expected_normalized

        # Проверяем соответствие: если actual_type — это валидный тип из маппинга, считаем OK
        # Но только если он не "Дополнительные материалы"
        if actual_normalized == "Дополнительные материалы":
            type_compliance["status"] = "не соответствует"
            issues = type_compliance.get("issues", [])
            issues.append("Не удалось определить тип документа.")
            type_compliance["issues"] = issues
            conclusion_suffix = "Тип документа не распознан."
        else:
            # Считаем, что тип определён верно
            type_compliance["status"] = "соответствует"
            type_compliance["issues"] = []
            conclusion_suffix = f"Тип документа подтверждён: {actual_normalized}."

        # Обновляем заключение
        old_conclusion = result.get("conclusion", "")
        result["conclusion"] = f"{conclusion_suffix} {old_conclusion}".strip()

        # Возвращаем полный результат
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


async def check_documents(
    file_paths: List[str],
    original_filenames: List[str],
    legislation: str = "44-ФЗ",
    procurement_method: str = "Конкурс",
    expertise_details: str = "Полный комплект документов о закупке"
) -> Dict[str, Any]:
    """
    Асинхронно анализирует набор загруженных документов на соответствие типу и содержанию.

    Функция последовательно обрабатывает каждый файл: извлекает текст, определяет его реальный тип
    с помощью языковой модели, нормализует тип по внутреннему маппингу и сохраняет результаты анализа.
    Не полагается на имя файла — тип определяется исключительно по содержимому. Поддерживает проверку
    в контексте законодательства (например, 44-ФЗ) и способа закупки.

    Args:
        file_paths (List[str]): Список путей к временным файлам на диске.
        original_filenames (List[str]): Оригинальные имена файлов (для логирования и отображения).
        legislation (str, optional): Нормативно-правовой акт, регулирующий закупку (например, '44-ФЗ', '223-ФЗ'). 
            По умолчанию — "44-ФЗ".
        procurement_method (str, optional): Способ проведения закупки (например, 'Конкурс', 'Аукцион'). 
            Передаётся в модель для контекстной оценки. По умолчанию — "Конкурс".
        expertise_details (str, optional): Дополнительная информация о цели экспертизы. 
            Может использоваться в будущем для уточнения промптов. По умолчанию — "Полный комплект документов о закупке".

    Raises:
        ValueError: Если документ пустой, не удаётся прочитать или возникает ошибка при обработке.

    Returns:
        Dict[str, Any]: Словарь с результатами проверки, содержащий:
            - status (str): Общий статус ('allow' — разрешён, может быть расширен в будущем).
            - provided_documents (List[str]): Список нормализованных типов обнаруженных документов.
            - missing_documents (List[str]): Пока не используется (зарезервировано для будущего).
            - documents_content (Dict[str, str]): Краткое содержание первых 300 символов каждого документа по типу.
            - type_compliance_issues (List): Проблемы с соответствием типа (пока не заполняется напрямую).
            - errors (List[str]): Сообщения об ошибках при обработке отдельных файлов.
    """
    results = {
        "status": "allow",
        "provided_documents": [],
        "missing_documents": [],
        "documents_content": {},
        "type_compliance_issues": [],
        "errors": []
    }

    for file_path, original_filename in zip(file_paths, original_filenames):
        basename = os.path.basename(original_filename)

        try:
            # Читаем текст
            text = read_file(file_path, original_filename=basename)
            if not text.strip():
                raise ValueError("Пустой документ")

            declared_type = "Определяется автоматически"

            # Анализируем
            analysis_result = await analyze_single_document(
                content=text,
                document_name=basename,
                document_type=declared_type,
                law_type=legislation,
                procurement_method=procurement_method
            )

            if analysis_result["status"] != "success":
                results["errors"].append(f"{basename}: {analysis_result['analysis']['conclusion']}")
                continue

            # Извлекаем actual_type, определённый моделью
            actual_type = analysis_result["analysis"]["type_compliance"].get("actual_type", "").strip()

            # Нормализуем через наш mapping
            from configs.utils import normalize_document_type
            final_doc_type = normalize_document_type(actual_type)

            if final_doc_type == "Дополнительные материалы":
                logger.warning(f"Тип не распознан для {basename}, используется fallback")
            else:
                logger.info(f"Определён тип: '{final_doc_type}' для файла {basename}")

            # Обновляем результат: чтобы в выводе было ясно
            analysis_result["analysis"]["type_compliance"]["expected_type"] = declared_type
            analysis_result["analysis"]["type_compliance"]["actual_type"] = final_doc_type

            # Если тип определён — считаем, что соответствует
            if final_doc_type != "Дополнительные материалы":
                analysis_result["analysis"]["type_compliance"]["status"] = "соответствует"
                analysis_result["analysis"]["type_compliance"]["issues"] = []

            # СОХРАНЯЕМ ПО ACTUAL_TYPE, А НЕ ПО ИМЕНИ ФАЙЛА
            save_raw_data(procurement_id="", document_type=final_doc_type, full_analysis=analysis_result["analysis"])
            save_clean_conclusion(procurement_id="", document_type=final_doc_type, conclusion=analysis_result["analysis"]["conclusion"])

            # Для отчёта
            results["provided_documents"].append(final_doc_type)
            results["documents_content"][final_doc_type] = text[:300]

        except Exception as e:
            logger.error(f"Ошибка при обработке {basename}: {e}")
            results["errors"].append(str(e))

    return results


def normalize_document_type(doc_type: str) -> str:
    """
    Нормализует строковое название типа документа к единому стандартному формату.

    Функция приводит входную строку к нижнему регистру и проверяет её на точное или частичное
    совпадение с эталонными типами документов. Возвращает каноническое имя типа, определённое
    в системе (например, "Техническое задание"). Если соответствие не найдено, возвращается
    значение по умолчанию.

    Args:
        doc_type (str): Исходное название типа документа, которое может быть написано в произвольной форме.

    Returns:
        str: Нормализованное название типа документа из предопределённого списка.
             Допустимые значения: 
                - "Требования к содержанию заявки на конкурс"
                - "Техническое задание"
                - "Извещение"
                - "Проект контракта"
             При отсутствии соответствия возвращает "Дополнительные материалы".
    """
    if not doc_type or not isinstance(doc_type, str):
        return "Дополнительные материалы"

    doc_type_clean = doc_type.strip().lower()

    # Полная проверка на точное совпадение
    for key in DOCUMENT_TYPE_MAPPING:
        if doc_type_clean == key.lower().strip():
            return key

    # Частичное совпадение
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


CONSISTENCY_FIELDS = [
    "procurement_object",        # Наименование объекта закупки
    "initial_contract_price",    # НМЦК
    "customer_name",             # Заказчик
    "contract_period",           # Срок исполнения
    "delivery_address",          # Адрес поставки
]

def check_consistency(procurement_id: str) -> Dict[str, Any]:
    """
    Проверяет согласованность данных по закупке: сравнивает одноимённые поля,
    извлечённые из разных документов.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.

    Returns:
        Dict[str, Any]: {
            "status": "ok" | "error",
            "issues": List[Tuple[field, value1, doc1, value2, doc2]],
            "conclusion": str
        }
    """
    try:
        raw_data = get_raw_data_by_procurement_id(procurement_id)
        if not raw_data:
            logger.warning(f"Нет данных в raw_document_data для закупки {procurement_id}")
            return {
                "status": "ok",
                "issues": [],
                "conclusion": "Проверка согласованности данных не проводилась: отсутствуют извлечённые данные."
            }

        issues = []
        field_values = {}  # {field: [(value, document_type)]}

        for doc_type, data in raw_data.items():
            if not isinstance(data, dict):
                continue
            for field in CONSISTENCY_FIELDS:
                if field in data:
                    value = data[field]
                    if value is None:
                        continue
                    field_values.setdefault(field, []).append((str(value).strip(), doc_type))

        # Проверяем расхождения
        for field, values_docs in field_values.items():
            if len(set(val for val, _ in values_docs)) > 1:
                unique_values = {}
                for val, doc in values_docs:
                    unique_values.setdefault(val, []).append(doc)

                # Форматируем проблему
                details = "; ".join([f'"{val}" ({", ".join(docs)})' for val, docs in unique_values.items()])
                issues.append({
                    "field": field,
                    "details": details
                })

        # Формируем заключение
        if not issues:
            conclusion = "Все ключевые поля в документах согласованы."
            status = "ok"
        else:
            conclusion_lines = ["Обнаружены расхождения в следующих полях:"]
            for issue in issues:
                conclusion_lines.append(f"- {issue['field']}: {issue['details']}")
            conclusion = "\n".join(conclusion_lines)
            status = "error"

        logger.info(f"Проверка согласованности завершена для {procurement_id}. Статус: {status}")

        return {
            "status": status,
            "issues": issues,
            "conclusion": conclusion
        }

    except Exception as e:
        logger.error(f"Ошибка при проверке согласованности для {procurement_id}: {e}", exc_info=True)
        return {
            "status": "error",
            "issues": [],
            "conclusion": f"Ошибка при проверке согласованности: {str(e)}"
        }
import os
import logging
import tempfile
import json
import re
from typing import List, Dict, Any
from openai import OpenAI
from dotenv import load_dotenv
from configs.utils import read_file, read_pdf_file
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация клиента OpenAI
client = OpenAI(
    base_url=os.getenv("M_MODEL_API_URL"),
    api_key=os.getenv("M_MODEL_API_KEY")
)
model_name = os.getenv("M_MODEL_NAME")

# === Упрощённая JSON-схема (только для одного документа) ===
RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "type_compliance": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["соответствует", "не соответствует"]},
                "issues": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["status", "issues", "recommendations"]
        },
        "readability": {
            "type": "object",
            "properties": {
                "is_readable": {"type": "boolean"},
                "language": {"type": "string"},
                "issues": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["is_readable", "issues"]
        }
    },
    "required": ["type_compliance", "readability"]
}

# === Упрощённый системный промпт (без комплектности) ===
SYSTEM_PROMPT = """
Ты — эксперт по проверке документов закупок. Проанализируй один документ и ответь строго в JSON.
Проверь:
1. **ТИП ДОКУМЕНТА**: соответствует ли документ своему типу (например, «Извещение», «Проект контракта»)?
   - Если нет — укажи причины.
2. **ЧИТАЕМОСТЬ**: можно ли прочитать текст? Язык? Проблемы с качеством?
   - Укажи, есть ли размытые страницы, водяные знаки, низкое разрешение.
Формат вывода: только чистый JSON, без пояснений.
Пример:
{
  "type_compliance": {
    "status": "соответствует",
    "issues": [],
    "recommendations": []
  },
  "readability": {
    "is_readable": true,
    "language": "русский",
    "issues": []
  }
}
"""


def extract_json_safely(text: str) -> dict:
    """Безопасно извлекает JSON из строки, даже если он обёрнут или повреждён."""
    try:
        # Удаляем markdown
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```\s*', '', text)
        # Находим первую и последнюю фигурные скобки
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace == -1 or last_brace == -1:
            raise ValueError("No JSON object found")
        text = text[first_brace:last_brace + 1]
        # Исправляем типичные ошибки
        text = re.sub(r',\s*}', '}', text)  # Убираем запятые перед }
        text = re.sub(r',\s*\]', ']', text)  # Убираем запятые перед ]
        text = re.sub(r"'([^']+)'(?=\s*:)", r'"\1"', text)  # Ключи в двойные кавычки
        text = re.sub(r":\s*'([^']*)'", r': "\1"', text)     # Значения в двойные кавычки
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON Decode Error: {e} | Raw: {repr(text)}")
        raise


def analyze_single_document(content: str, doc_name: str, doc_type: str, law_type: str, procurement_method: str) -> Dict[str, Any]:
    """Анализ одного документа через модель с упрощённой схемой."""
    try:
        prompt = f"""
Тип закупки: {law_type}
Способ закупки: {procurement_method}
Имя файла: {doc_name}
Ожидаемый тип: {doc_type}
Содержимое (первые 4000 символов):
{content[:4000]}
"""

        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=1024,
            extra_body={"guided_json": RESPONSE_JSON_SCHEMA}
        )

        content = completion.choices[0].message.content.strip()
        result = extract_json_safely(content)

        # Валидация структуры
        if not isinstance(result, dict):
            raise ValueError("Response is not a JSON object")

        if "type_compliance" not in result or "readability" not in result:
            raise ValueError("Missing required fields: type_compliance or readability")

        # Приведём статус к нужному виду
        status = result["type_compliance"].get("status", "не соответствует")
        if status not in ["соответствует", "не соответствует"]:
            result["type_compliance"]["status"] = "не соответствует"

        # Нормализуем списки
        for field in ["issues", "recommendations"]:
            if field not in result["type_compliance"] or not isinstance(result["type_compliance"][field], list):
                result["type_compliance"][field] = []
        for field in ["issues"]:
            if field not in result["readability"] or not isinstance(result["readability"][field], list):
                result["readability"][field] = []

        return result

    except Exception as e:
        logger.error(f"Ошибка при анализе документа {doc_name}: {e}")
        return {
            "type_compliance": {
                "status": "не соответствует",
                "issues": ["Ошибка анализа: не удалось получить ответ от модели"],
                "recommendations": ["Проверьте документ вручную"]
            },
            "readability": {
                "is_readable": False,
                "language": "неизвестно",
                "issues": ["Ошибка обработки"]
            }
        }


def check_documents(
    files: List,
    legislation: str = "44-ФЗ",
    procurement_method: str = "Конкурс",
    expertise_details: str = "Полный комплект документов о закупке"
) -> Dict[str, Any]:
    if not files:
        return {"error": "Необходимо загрузить хотя бы один документ"}

    document_analysis = []
    for file in files:
        # Правильно извлекаем оригинальное имя
        original_filename = getattr(file, 'name', '')
        basename = os.path.basename(original_filename)
        doc_type = "Документ"
        for key in DOCUMENT_TYPE_MAPPING.keys():
            if key.lower() in basename.lower():
                doc_type = key
                break

        try:
            with open(file.name, "rb") as f:
                file_data = f.read()

            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
                tmp.write(file_data)
                tmp_path = tmp.name

            try:
                # Передаём оригинальное имя файла
                text = read_file(tmp_path, original_filename=basename)
            finally:
                os.unlink(tmp_path)

            analysis = analyze_single_document(text, basename, doc_type, legislation, procurement_method)
            analysis["document_name"] = basename
            document_analysis.append(analysis)

        except Exception as e:
            logger.error(f"Ошибка при обработке файла {basename}: {str(e)}")
            document_analysis.append({
                "document_name": basename,
                "type_compliance": {"status": "не соответствует", "issues": [str(e)], "recommendations": []},
                "readability": {"is_readable": False, "language": "неизвестно", "issues": ["Ошибка чтения файла"]},
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
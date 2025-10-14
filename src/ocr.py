import base64
import os
import logging

from openai import OpenAI

from configs.config import Config


logger = logging.getLogger(__name__)


client = OpenAI(
    base_url=Config.M_MODEL_API_URL,
    api_key=Config.M_MODEL_API_KEY
)


def ocr_image_with_qwen_vl(image_path: str) -> str:
    """
    Распознаёт текст на изображении с использованием мультимодальной модели Qwen-VL.
    Дополнительно возвращает мета-информацию о качестве изображения и процессе распознавания.

    Args:
        image_path (str): Путь к файлу изображения.

    Returns:
        str: JSON-строка с распознанным текстом и метаданными о качестве обработки.
    """
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')

    try:
        response = client.chat.completions.create(
            model=Config.M_MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": """Ты выполняешь OCR-обработку документа. Проанализируй изображение и верни ответ в формате JSON со следующей структурой:
                        {
                          "raw_text": "весь распознанный текст, объединенный в одну строку",
                          "processing_quality": {
                            "status": "high|medium|low",
                            "issues": ["список проблем", "например: низкая контрастность, размытый текст на странице 2"]
                          },
                          "language_hints": ["русский", "английский"]
                        }
                        Не добавляй никакого форматирования в raw_text."""},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ],
            max_tokens=4096  # Увеличил лимит для длинных документов
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OCR ошибка при обработке {image_path}: {e}")
        # Возвращаем JSON с ошибкой для сохранения структуры
        return '{"raw_text": "", "processing_quality": {"status": "error", "issues": ["Ошибка при распознавании изображения"]}, "language_hints": []}'
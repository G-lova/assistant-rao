import base64
import os
import logging

from openai import OpenAI


logger = logging.getLogger(__name__)


client = OpenAI(
    base_url=os.getenv("M_MODEL_API_URL"),
    api_key=os.getenv("M_MODEL_API_KEY")
)


def ocr_image_with_qwen_vl(image_path: str) -> str:
    """
    Распознаёт текст на изображении с использованием мультимодальной модели Qwen-VL.

    Функция кодирует изображение в формат Base64 и отправляет его вместе с запросом
    на распознавание текста в модель Qwen-VL через API. Ожидается, что модель вернёт
    извлечённый текст.

    Args:
        image_path (str): Путь к файлу изображения (поддерживаются, например, JPEG, PNG).

    Returns:
        str: Распознанный и отредактированный текст с изображения.
             В случае ошибки возвращается сообщение об ошибке в квадратных скобках.
    """
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')

    try:
        response = client.chat.completions.create(
            model=os.getenv("M_MODEL_NAME"),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Распознай текст на этом изображении. Верни только текст и отредактируй его по необходимости."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ],
            max_tokens=1024
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OCR ошибка при обработке {image_path}: {e}")
        return "[Ошибка при распознавании изображения]"
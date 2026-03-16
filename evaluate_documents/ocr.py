import aiofiles
import base64
import json

from configs.llm_client import get_llm
from configs.logger import get_logger


logger = get_logger(__name__)


async def ocr_image_with_qwen_vl(image_path: str) -> str:
    """
    Распознаёт текст на изображении с использованием мультимодальной модели Qwen-VL.
    Дополнительно возвращает мета-информацию о качестве изображения и процессе распознавания.

    Args:
        image_path (str): Путь к файлу изображения.

    Returns:
        str: JSON-строка с распознанным текстом и метаданными о качестве обработки.
    """

    # Загрузка конфигурации
    client, model = get_llm()

    async with aiofiles.open("prompts/ocr_prompt.txt") as f:
        ocr_prompt = await f.read()

    async with aiofiles.open("schemas/ocr_schema.json") as f:
        OCR_SCHEMA = json.loads(await f.read())

    async with aiofiles.open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(await image_file.read()).decode('utf-8')

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ocr_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            extra_body={"guided_json": OCR_SCHEMA},
            max_tokens=4096,  # Увеличил лимит для длинных документов
            temperature=0.1
        )
        return response.choices[0].message.content.strip()
    
    except Exception as e:
        logger.error(f"OCR ошибка при обработке {image_path}: {e}")
        # Возвращаем JSON с ошибкой для сохранения структуры
        return """
            {
                "has_text": false,
                "raw_text": "",
                "visual_notes": "",
                "image_description": "",
                "processing_quality": {
                    "status": "error",
                    "ocr_score": 0.0,
                    "issues": ["Ошибка при анализе изображения"]
                }
            }
        """.strip()
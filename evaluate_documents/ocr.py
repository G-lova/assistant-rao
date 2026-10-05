import asyncio
import aiofiles
import base64
import json

from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry


logger = get_logger(__name__)


class OCRProcessor:
    """
    """
    def __init__(self, llm_client, model):
        """
        """
        self.client = llm_client
        self.model = model

        with open("prompts/ocr_prompt.txt") as f:
            self.ocr_prompt = f.read()

        with open("schemas/ocr_schema.json") as f:
            self.OCR_SCHEMA = json.loads(f.read())

        # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду


    @async_retry(LLM_RETRY_CONFIG)
    async def ocr_image_with_qwen_vl(self, image_path: str, original_filename: str) -> str:
        """
        Распознаёт текст на изображении с использованием мультимодальной модели Qwen-VL.
        Дополнительно возвращает мета-информацию о качестве изображения и процессе распознавания.

        Args:
            image_path (str): Путь к файлу изображения.

        Returns:
            str: JSON-строка с распознанным текстом и метаданными о качестве обработки.
        """
        await self.rate_limiter.acquire()   

        async with aiofiles.open(image_path, "rb") as image_file:
            base64_image = base64.b64encode(await image_file.read()).decode('utf-8')

        image_name = image_path.split("/")[-1]
        logger.info(f"OCR обработка {original_filename}/{image_name}")

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.ocr_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                }
                            }
                        ]
                    }
                ],
                extra_body={"guided_json": self.OCR_SCHEMA},
                max_tokens=2048,  # Увеличил лимит для длинных документов
                temperature=0.1
            )

            raw_response = response.choices[0].message.content.strip()
            logger.info(f'OCR response для {original_filename}/{image_name}: {raw_response}')
            return raw_response
        
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при OCR: {original_filename}/{image_name}")
            return '{"status": "error", "error": "Таймаут при анализе изображения"}'
        
        except Exception as e:
            logger.error(f"OCR ошибка при обработке {original_filename}/{image_name}: {e}")
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
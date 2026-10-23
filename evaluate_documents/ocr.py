import asyncio
import aiofiles
import base64
import json
import re

from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry


logger = get_logger(__name__)

OCR_MAX_PIXELS = 1700 * 2400   # как risk_monitoring.pdf_text: больше — не помещается в контекст модели
OCR_SHRINK_TRIES = 3
CONTEXT_RE = re.compile(r"maximum context length|exceeds .*context|context length", re.IGNORECASE)


def fit_image_file(path: str, max_pixels: int = OCR_MAX_PIXELS) -> None:
    """Уменьшает изображение на месте до ``max_pixels`` пикселей (пропорционально); меньшие и нечитаемые — как есть."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
            if w * h <= max_pixels:
                return
            k = (max_pixels / float(w * h)) ** 0.5
            small = im.convert("RGB").resize((max(1, int(w * k)), max(1, int(h * k))))
        small.save(path, "JPEG", quality=90)
    except Exception as e:  # noqa: BLE001 — распознаём как есть
        logger.warning(f"OCR: изображение {path} не уменьшено: {type(e).__name__}: {e}")


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

        image_name = image_path.split("/")[-1]
        await asyncio.to_thread(fit_image_file, image_path)
        for attempt in range(OCR_SHRINK_TRIES + 1):
            raw, overflow = await self._ocr_once(image_path, original_filename, image_name)
            if not overflow or attempt == OCR_SHRINK_TRIES:
                return raw
            # изображение не помещается в контекст модели (крупный формат: чертежи ПСД в архиве) — это не сбой
            # модели: уменьшаем и повторяем (прогон 02.07.2026: «Input length (17011) exceeds … (16384)»)
            from risk_monitoring.pdf_text import OCR_SHRINK, shrink_image

            size = await asyncio.to_thread(shrink_image, image_path, OCR_SHRINK)
            logger.warning(f"OCR {original_filename}/{image_name}: изображение больше контекста модели — "
                           f"уменьшено до {size[0]}×{size[1]}, повтор")
        return raw

    async def _ocr_once(self, image_path: str, original_filename: str, image_name: str):
        """Один запрос распознавания.

        Returns:
            tuple: (JSON-строка ответа или ошибки, переполнен ли контекст модели).
        """
        async with aiofiles.open(image_path, "rb") as image_file:
            base64_image = base64.b64encode(await image_file.read()).decode('utf-8')

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
            return raw_response, False
        
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при OCR: {original_filename}/{image_name}")
            return '{"status": "error", "error": "Таймаут при анализе изображения"}', False
        
        except Exception as e:
            overflow = bool(CONTEXT_RE.search(str(e)))
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
            """.strip(), overflow
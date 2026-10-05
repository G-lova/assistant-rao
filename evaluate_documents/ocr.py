import asyncio
import aiofiles
import base64
import json
import openai
from PIL import Image

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

        logger.info(f"OCR обработка {original_filename}")

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
                    
            # # Обработка 504 (TimeoutError) — Retry-After или экспоненциальная задержка
            # if response.status == 504:
            #     retry_after = response.headers.get('Retry-After')
            #     retry_after_sec = int(retry_after) if retry_after else None
            #     raise TimeoutError(
            #         f"OCR timeout", 
            #         retry_after=retry_after_sec
            #     )   

            raw_response = response.choices[0].message.content.strip()
            logger.info(f'OCR response для {original_filename}: {raw_response}')
            return raw_response
        
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при OCR: {original_filename}")
            return '{"status": "error", "error": "Таймаут при анализе изображения"}'
        
        # except openai.BadRequestError as e:
        #     if "context_length_exceeded" in str(e) or "token" in str(e).lower():
        #         logger.warning("Превышен лимит токенов. Повтор с пониженным качеством изображения.")
        #         # return await _retry_ocr_with_fallback(image_path, ocr_prompt, OCR_SCHEMA, model, client)
        #         return await _retry_ocr_with_fallback(image_path, original_filename)
        #     raise
        
        except Exception as e:
            logger.error(f"OCR ошибка при обработке {original_filename}: {e}")
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
        

    # async def _retry_ocr_with_fallback(image_path, prompt, schema, model, client):
    #     import io
        
    #     # Сжимаем изображение до 1024px по широкой стороне
    #     img = Image.open(image_path)
    #     img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        
    #     buffer = io.BytesIO()
    #     img.save(buffer, format="JPEG", quality=75, optimize=True)
    #     base64_resized = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
    #     try:
    #         response = await client.chat.completions.create(
    #             model=model,
    #             messages=[{"role": "user", "content": [
    #                 {"type": "text", "text": prompt},
    #                 {"type": "image_url", "image_url": {"url": f"image/jpeg;base64,{base64_resized}", "detail": "low"}}
    #             ]}],
    #             extra_body={"guided_json": schema},
    #             max_tokens=1500,
    #             temperature=0.1
    #         )
    #         return response.choices[0].message.content.strip()
    #     except Exception as e:
    #         logger.error(f"Fallback OCR failed: {e}")
    #         return '{"has_text": false, "raw_text": "", "visual_notes": "", "image_description": "", "processing_quality": {"status": "error", "issues": ["Превышен лимит контекста"]}}'
        


    def split_image_into_chunks(image_path, rows=2, cols=1):
        img = Image.open(image_path)
        w, h = img.size
        chunk_h = h // rows
        paths = []
        for i in range(rows):
            box = (0, i * chunk_h, w, (i + 1) * chunk_h)
            chunk = img.crop(box)
            tmp_path = f"{image_path}_chunk_{i}.jpg"
            chunk.save(tmp_path, "JPEG", quality=85)
            paths.append(tmp_path)
        return paths


    async def _retry_ocr_with_fallback(self, image_path: str, original_filename: str):

        image_paths = self.split_image_into_chunks(image_path, original_filename)
        ocr_results = await asyncio.gather(*(self.ocr_image_with_qwen_vl(path, original_filename) for path in image_paths), return_exceptions=True)
        successful_results = [r for r in ocr_results if not isinstance(r, Exception)]

        return "\n".join(successful_results)
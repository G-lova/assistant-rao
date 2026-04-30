import asyncio
import aiofiles
import base64
import json
import os
import openai
from PIL import Image
from typing import Optional, List

from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.rate_limiter import TokenBucket 
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry

logger = get_logger(__name__)

# 1. Глобальный лимитер (синглтон)
_global_rate_limiter = TokenBucket(rate=1.5)

# 2. Кэширование статики при старте модуля
_OCR_PROMPT_CACHE: Optional[str] = None
_OCR_SCHEMA_CACHE: Optional[dict] = None

async def _load_static_assets():
    global _OCR_PROMPT_CACHE, _OCR_SCHEMA_CACHE
    if _OCR_PROMPT_CACHE is None:
        async with aiofiles.open("prompts/ocr_prompt.txt") as f:
            _OCR_PROMPT_CACHE = await f.read()
        async with aiofiles.open("schemas/ocr_schema.json") as f:
            _OCR_SCHEMA_CACHE = json.loads(await f.read())

def _make_error_response(error_msg: str, issues: List[str]) -> str:
    """Гарантированно возвращает валидный JSON-строку"""
    return json.dumps({
        "has_text": False,
        "raw_text": "",
        "visual_notes": "",
        "image_description": f"Error: {error_msg}",
        "processing_quality": {"status": "error", "issues": issues}
    }, ensure_ascii=False)

async def _call_llm_raw(client, model: str, prompt: str, image_b64: str, schema: dict, max_tokens: int):
    """Низкоуровневый вызов БЕЗ обработки ошибок (для использования в retry/fallback)"""
    return await client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            ]
        }],
        extra_body={"guided_json": schema},
        max_tokens=max_tokens,
        temperature=0.1
    )

def _split_image_smart(image_path: str, max_height: int = 2000) -> List[str]:
    """Делит изображение, если оно слишком высокое. Возвращает список путей. Удаляет временные файлы."""
    img = Image.open(image_path)
    w, h = img.size
    
    if h <= max_height:
        return [image_path]
    
    # Делим на 2 части с небольшим перекрытием (overlap), чтобы не разрезать строки
    overlap = 50 
    mid = h // 2
    chunks = [
        img.crop((0, 0, w, mid + overlap)),
        img.crop((0, mid - overlap, w, h))
    ]
    
    paths = []
    try:
        for i, chunk in enumerate(chunks):
            # Сохраняем во временный буфер или файл с уникальным именем
            tmp_path = f"{image_path}.chunk_{i}.jpg"
            chunk.save(tmp_path, "JPEG", quality=85)
            paths.append(tmp_path)
        return paths
    except Exception as e:
        # При ошибке чистим созданные файлы
        for p in paths:
            if os.path.exists(p): os.remove(p)
        raise e

@async_retry(LLM_RETRY_CONFIG)
async def ocr_image_with_qwen_vl(image_path: str, original_filename: str, _is_fallback: bool = False) -> str:
    """
    Основной метод OCR.
    _is_fallback: внутренний флаг для предотвращения бесконечной рекурсии
    """
    await _load_static_assets()
    client, model = get_llm()
    
    # Глобальный рейт-лимит
    await _global_rate_limiter.acquire()

    try:
        async with aiofiles.open(image_path, "rb") as img_file:
            base64_image = base64.b64encode(await img_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to read image {image_path}: {e}")
        return _make_error_response("ReadError", [str(e)])

    try:
        response = await _call_llm_raw(
            client, model, _OCR_PROMPT_CACHE, base64_image, _OCR_SCHEMA_CACHE, max_tokens=2048
        )
        return response.choices[0].message.content.strip()

    except openai.BadRequestError as e:
        err_str = str(e).lower()
        # Если лимит токенов и мы ещё не в фоллбэке -> режем картинку
        if ("context_length" in err_str or "token" in err_str) and not _is_fallback:
            logger.warning(f"Token limit hit for {original_filename}. Splitting image...")
            
            chunks = _split_image_smart(image_path)
            if len(chunks) == 1:
                # Не удалось разбить, возвращаем ошибку
                return _make_error_response("ContextLimit", ["Image too complex to split"])

            # Обрабатываем чанки параллельно
            tasks = [ocr_image_with_qwen_vl(c, original_filename, _is_fallback=True) for c in chunks]
            semaphore = asyncio.Semaphore(3)
            async with semaphore:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 1. Очистка временных файлов
            for c in chunks:
                if c != image_path and os.path.exists(c):
                    os.remove(c)

            # 2. Агрегация результатов
            valid_texts = []
            for r in results:
                if isinstance(r, Exception):
                    logger.error(f"Chunk OCR failed: {r}")
                    continue
                try:
                    # Пытаемся распарсить, чтобы вытащить только raw_text для склейки
                    data = json.loads(r) if isinstance(r, str) else r
                    valid_texts.append(data.get("raw_text", ""))
                except json.JSONDecodeError:
                    valid_texts.append(str(r)) # fallback к строке
            
            # Возвращаем валидный JSON с объединённым текстом
            return json.dumps({
                "has_text": True,
                "raw_text": "\n---[PAGE BREAK]---\n".join(valid_texts),
                "visual_notes": "Reconstructed from chunks",
                "image_description": "",
                "processing_quality": {"status": "partial", "issues": ["Split due to token limit"]}
            }, ensure_ascii=False)
        
        raise # Пробрасываем ошибку выше, если это не токен-лимит

    except asyncio.TimeoutError:
        logger.error(f"Timeout OCR: {original_filename}")
        return _make_error_response("Timeout", ["Request timed out"])
    
    except Exception as e:
        logger.error(f"Unexpected OCR error {original_filename}: {e}", exc_info=True)
        return _make_error_response("InternalError", [str(e)])
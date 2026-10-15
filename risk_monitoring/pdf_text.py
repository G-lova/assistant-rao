"""Чтение PDF для риск-мониторинга: сначала текстовый слой, OCR — только для страниц без текста.

Общий ``FileReader.read_pdf_file`` распознаёт **все** страницы PDF мультимодальной моделью (140 dpi,
2048 токенов, без штрафа за повторы), даже если в PDF есть текстовый слой. На плотных таблицах
(обоснование НМЦК) модель зацикливается: «2,44 2,44 …» (файл 1097). Здесь, не меняя общий
компонент экспертизы:

1. Текст берётся из текстового слоя PDF (PyPDF2, иначе pdfplumber). Страница с текстовым слоем
   принимается, если в ней достаточно букв, нет ``(cid:NN)`` и «кракозябр» от неверной кодировки шрифта.
2. Страницы без пригодного слоя (сканы) рендерятся в 200 dpi и распознаются моделью с
   ``repetition_penalty`` и бо́льшим лимитом токенов; если в ответе цикл повторов (``text_quality``),
   страница распознаётся повторно с более сильным штрафом, а затем — вдвое уменьшенными половинками.
3. Разметка страниц совместима с ``knowledge_store.pages.PAGE_MARKER_RE``: ``Страница N:`` для слоя,
   ``Страница N (OCR):`` для распознанных страниц.
"""
import asyncio
import base64
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from configs.logger import get_logger
from risk_monitoring import text_quality

logger = get_logger(__name__)

MIN_PAGE_LETTERS = 40          # меньше букв на странице — считаем, что слоя нет (скан)
MIN_GOOD_SHARE = 0.85          # доля «нормальных» символов в тексте слоя
OCR_DPI = 200
OCR_MAX_TOKENS = 4096
OCR_PENALTIES = (1.05, 1.15)   # repetition_penalty: первая попытка и повтор при цикле

GOOD_CHARS_RE = re.compile(r"[А-Яа-яЁёA-Za-z0-9\s.,;:!?()\[\]{}«»\"'№%/\\+\-–—=*<>≤≥°_|#&@$€₽^~`]")
CID_RE = re.compile(r"\(cid:\d+\)")


def layer_page_ok(text: str) -> bool:
    """Пригоден ли текст страницы из текстового слоя.

    Args:
        text: Текст страницы.

    Returns:
        bool: ``True`` — достаточно букв, нет ``(cid:NN)``, ≥ 85 % «нормальных» символов.
    """
    text = text or ""
    if CID_RE.search(text) or len(text_quality.LETTERS_RE.findall(text)) < MIN_PAGE_LETTERS:
        return False
    stripped = re.sub(r"\s+", "", text)
    good = len(GOOD_CHARS_RE.findall(stripped))
    return bool(stripped) and good / len(stripped) >= MIN_GOOD_SHARE


def text_layer(path: str) -> List[str]:
    """Текст каждой страницы из текстового слоя PDF.

    Args:
        path: Путь к PDF.

    Returns:
        List[str]: Текст по страницам (пустая строка — слоя нет); пустой список, если PDF не читается.
    """
    try:
        from PyPDF2 import PdfReader  # есть в requirements

        reader = PdfReader(path)
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:  # noqa: BLE001 — пробуем pdfplumber
        logger.info(f"pdf_text: PyPDF2 не прочитал {path}: {type(e).__name__}: {e}")
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return [(page.extract_text() or "") for page in pdf.pages]
    except Exception as e:  # noqa: BLE001 — слоя нет, всё распознаём
        logger.info(f"pdf_text: pdfplumber не прочитал {path}: {type(e).__name__}: {e}")
        return []


def ocr_text(raw: str) -> str:
    """Текст из ответа OCR (JSON ``raw_text`` или строка как есть).

    Args:
        raw: Ответ модели.

    Returns:
        str: Распознанный текст.
    """
    from risk_monitoring.llm_json import parse_json

    try:
        data = parse_json(raw)
        if isinstance(data, dict):
            return str(data.get("raw_text") or "")
    except Exception:  # noqa: BLE001 — ответ не JSON
        pass
    return raw or ""


def ocr_schema(processor: Any) -> Dict[str, Any]:
    """Схема ответа OCR: схема ``OCRProcessor`` с исправленным ``required``.

    В ``schemas/ocr_schema.json`` в ``required`` указано поле ``readability``, которого нет в
    ``properties``, а ``processing_quality`` не обязательно. Здесь — по промпту.

    Args:
        processor: ``OCRProcessor``.

    Returns:
        Dict[str, Any]: JSON-схема.
    """
    schema = json.loads(json.dumps(getattr(processor, "OCR_SCHEMA", {}) or {}))
    props = schema.get("properties") or {}
    schema["required"] = [k for k in ("has_text", "raw_text", "visual_notes", "image_description",
                                      "processing_quality") if k in props]
    return schema


async def ocr_image(processor: Any, image_path: str, file_name: str, penalty: float) -> str:
    """Распознаёт одно изображение моделью ``OCRProcessor`` со штрафом за повторы.

    Args:
        processor: ``OCRProcessor`` (``client``, ``model``, ``ocr_prompt``, ``OCR_SCHEMA``).
        image_path: Путь к JPEG.
        file_name: Имя исходного файла (для логов).
        penalty: ``repetition_penalty`` (vLLM).

    Returns:
        str: Распознанный текст (пустая строка при ошибке).
    """
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    try:
        if getattr(processor, "rate_limiter", None):
            await processor.rate_limiter.acquire()
        from risk_monitoring.llm_json import call_llm

        content = await call_llm(
            processor.client, model=processor.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": processor.ocr_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            extra_body={"guided_json": ocr_schema(processor), "repetition_penalty": penalty},
            max_tokens=OCR_MAX_TOKENS, temperature=0.0,
        )
        return ocr_text(content)
    except Exception as e:  # noqa: BLE001 — страница остаётся пустой, документ читается дальше
        logger.error(f"pdf_text: OCR {file_name}/{os.path.basename(image_path)}: {type(e).__name__}: {e}")
        return ""


async def ocr_page(processor: Any, image: Any, tmp: str, page_no: int, file_name: str) -> Tuple[str, str]:
    """Распознаёт страницу; при цикле повторов — повтор с бо́льшим штрафом, затем по половинам.

    Args:
        processor: ``OCRProcessor``.
        image: Страница (PIL.Image).
        tmp: Временная папка.
        page_no: Номер страницы (с 1).
        file_name: Имя файла.

    Returns:
        Tuple[str, str]: Текст и статус качества (``ok`` / ``suspect`` / ``bad``).
    """
    path = os.path.join(tmp, f"page_{page_no}.jpg")
    await asyncio.to_thread(image.convert("RGB").save, path, "JPEG", quality=90)
    text, status = "", "bad"
    for penalty in OCR_PENALTIES:
        text = await ocr_image(processor, path, file_name, penalty)
        q = text_quality.assess(text)
        status = q["status"] if q["loops"] else "ok"
        if not q["loops"]:
            return text, status
        logger.warning(f"pdf_text: {file_name}, стр. {page_no}: цикл повторов (penalty={penalty}), повтор")
    # Последняя попытка: верхняя и нижняя половины страницы — короче ответ, меньше риск зацикливания
    w, h = image.size
    parts = []
    for i, box in enumerate(((0, 0, w, h // 2 + 40), (0, h // 2 - 40, w, h))):
        half = os.path.join(tmp, f"page_{page_no}_{i}.jpg")
        await asyncio.to_thread(image.crop(box).convert("RGB").save, half, "JPEG", quality=90)
        parts.append(await ocr_image(processor, half, file_name, OCR_PENALTIES[-1]))
    halves = "\n".join(parts)
    q = text_quality.assess(halves)
    best = halves if q["loop_share"] <= text_quality.assess(text)["loop_share"] else text
    return text_quality.collapse_loops(best)[0], q["status"]


async def read_pdf(path: str, file_name: str, processor: Any, concurrency: int = 2) -> Optional[Dict[str, Any]]:
    """Текст PDF: слой там, где он пригоден, OCR — для остальных страниц.

    Args:
        path: Путь к PDF.
        file_name: Имя файла.
        processor: ``OCRProcessor`` (из ``FileReader.ocr_processor``); ``None`` — без OCR.
        concurrency: Одновременных OCR-запросов.

    Returns:
        Optional[Dict[str, Any]]: ``text``, ``pages``, ``layer_pages``, ``ocr_pages``, ``ocr_suspect_pages``;
        ``None`` — PDF не прочитан (вызывающий переходит на общий ``FileReader``).
    """
    pages = await asyncio.to_thread(text_layer, path)
    if not pages:
        return None
    need_ocr = [i for i, t in enumerate(pages) if not layer_page_ok(t)]
    if need_ocr and processor is None:
        return None
    out: Dict[int, str] = {i: f"Страница {i + 1}:\n{t.strip()}" for i, t in enumerate(pages) if i not in need_ocr}
    suspect: List[int] = []
    if need_ocr:
        from pdf2image import convert_from_path

        sem = asyncio.Semaphore(concurrency)
        with tempfile.TemporaryDirectory() as tmp:
            async def one(i: int) -> None:
                """Рендер и распознавание одной страницы."""
                async with sem:
                    imgs = await asyncio.to_thread(convert_from_path, path, dpi=OCR_DPI, first_page=i + 1, last_page=i + 1)
                    text, status = await ocr_page(processor, imgs[0], tmp, i + 1, file_name)
                    if status != "ok":
                        suspect.append(i + 1)
                    out[i] = f"Страница {i + 1} (OCR):\n{text.strip()}"

            await asyncio.gather(*(one(i) for i in need_ocr))
    text = "\n\n".join(out[i] for i in sorted(out))
    logger.info(f"pdf_text: {file_name}: страниц {len(pages)}, слой {len(pages) - len(need_ocr)}, OCR {len(need_ocr)}")
    return {"text": text, "pages": len(pages), "layer_pages": len(pages) - len(need_ocr),
            "ocr_pages": len(need_ocr), "ocr_suspect_pages": sorted(suspect)}

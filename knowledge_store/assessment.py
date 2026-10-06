"""Поля сводного ЭЗ, которые определяются оценкой модели по документам целиком, а не поиском фактов.

* ``field2_2_2_0`` — метод обоснования НМЦК (выбор): 1 — рыночный, 2 — нормативный, 3 — затратный,
  4 — тарифный, 5 — проектно-сметный. Сначала регулярное выражение по строке «Используемый метод определения НМЦК»,
  при неудаче — точечный вопрос модели по найденным окнам текста (значение принимается только с дословной цитатой).
* ``field2_2_1_6`` — единый стиль документации и отсутствие логических ошибок: модель просматривает документы по частям
  и называет конкретные дефекты с дословными цитатами; ответ «0» ставится только при подтверждённой цитате,
  иначе — «1» как предложение модели (``proposed``).

Модуль не зависит от ``asyncpg`` и сети; модель вызывается через переданную функцию ``llm_call``.
"""
import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from configs.logger import get_logger
from knowledge_store import facts as facts_mod

logger = get_logger(__name__)

LlmCall = Callable[[List[dict], dict], Awaitable[str]]

NMCK_CHOICE_KEY = "field2_2_2_0"
STYLE_KEY = "field2_2_1_6"
# Код выбора метода НМЦК в форме конкурса: основа слова → (код, название)
NMCK_METHOD_CODES = {"рыночн": (1, "рыночный"), "нормативн": (2, "нормативный"), "затратн": (3, "затратный"),
                     "тарифн": (4, "тарифный"), "проектно-сметн": (5, "проектно-сметный")}

CHUNK = 6000          # размер части документа для одного запроса, символов
MAX_DOCS = 6          # сколько документов просматривать
MAX_CHUNKS = 4        # сколько частей брать из документа (равномерно)

NMCK_SCHEMA = {"type": "object", "properties": {
    "method": {"type": ["string", "null"], "enum": ["рыночный", "нормативный", "затратный", "тарифный", "проектно-сметный", None]},
    "quote": {"type": ["string", "null"], "maxLength": 300}}, "required": ["method", "quote"], "additionalProperties": False}
NMCK_PROMPT = ("Ты — эксперт по закупкам (44-ФЗ). По фрагментам определи, КАКОЙ ОДИН метод определения начальной "
               "(максимальной) цены контракта применён заказчиком: рыночный (метод сопоставимых рыночных цен, анализ рынка), "
               "нормативный, затратный, тарифный или проектно-сметный. Если применено несколько методов, выбери основной. "
               "Используй только текст. Приведи ДОСЛОВНУЮ цитату (до 300 символов). Нет данных — method=null. Только JSON.")
STYLE_SCHEMA = {"type": "object", "properties": {"defects": {"type": "array", "maxItems": 3, "items": {
    "type": "object", "properties": {"quote": {"type": "string", "maxLength": 300}, "issue": {"type": "string", "maxLength": 300}},
    "required": ["quote", "issue"]}}}, "required": ["defects"], "additionalProperties": False}
STYLE_PROMPT = ("Ты — эксперт по закупкам (44-ФЗ). Проверь фрагмент документации на единый стиль и отсутствие логических "
                "ошибок. Дефект — ТОЛЬКО явный и подтверждаемый текстом: внутреннее противоречие (разные сроки, суммы, "
                "наименования одного и того же), ссылка на несуществующий пункт или приложение, упоминание другого "
                "заказчика, другой закупки или другого объекта (остатки чужого шаблона), незаполненные поля и "
                "плейсхолдеры («___», «[указать]»), несогласованные термины. Не считай дефектом особенности вёрстки, "
                "стиль изложения и отсутствие сведений. Для каждого дефекта приведи ДОСЛОВНУЮ цитату (до 300 символов) и "
                "коротко (одно предложение) опиши, в чём он состоит. Если дефектов нет, верни пустой список. Только JSON.")


def _parse(raw: str) -> Optional[dict]:
    """Ответ модели как объект (``None``, если разобрать нельзя)."""
    return facts_mod.parse_answer(raw)


def keyword_windows(documents: Dict[int, dict], pattern: str, radius: int = 350, limit: int = 4) -> List[str]:
    """Окна текста вокруг ключевых слов (сначала документы извещения)."""
    out: List[str] = []
    for doc in sorted(documents.values(), key=lambda d: d.get("doc_code") != "docIzvejenieFiles"):
        text = doc.get("text") or ""
        for m in re.finditer(pattern, text, flags=re.I):
            window = text[max(0, m.start() - 150):min(len(text), m.end() + radius)]
            if all(window[:60] not in w for w in out):
                out.append(window)
            if len(out) >= limit:
                return out
    return out


def _quote_source(documents: Dict[int, dict], quote: Any) -> Tuple[Optional[str], Optional[str]]:
    """Находит цитату в документах: ``(подтверждённый текст, имя файла)``."""
    if not quote:
        return None, None
    for doc in documents.values():
        found = facts_mod.find_quote(str(quote), doc.get("text") or "")
        if found:
            return found[:300], doc.get("filename")
    return None, None


async def detect_nmck_choice(documents: Dict[int, dict], llm_call: Optional[LlmCall]) -> Optional[dict]:
    """Определяет метод обоснования НМЦК для ``field2_2_2_0``.

    Args:
        documents: Документы экспертизы с текстами.
        llm_call: Функция вызова LLM или ``None``.

    Returns:
        Optional[dict]: ``{"value", "method", "quote", "document", "source"}`` или ``None``, если метод не найден.
    """
    from knowledge_store import summary          # отложенный импорт: summary импортирует этот модуль
    stem, snippet = summary.used_nmck_method(documents)
    if stem in NMCK_METHOD_CODES:
        code, name = NMCK_METHOD_CODES[stem]
        return {"value": code, "method": name, "quote": (snippet or "")[:300], "source": "text_rule"}
    if llm_call is None:
        return None
    windows = keyword_windows(documents, r"(?:метод\w*|методик\w*)[^\n]{0,80}Н\(?М?\)?ЦК|Н\(?М?\)?ЦК[^\n]{0,80}метод\w*")
    if not windows:
        return None
    try:
        parsed = _parse(await llm_call([{"role": "system", "content": NMCK_PROMPT},
                                        {"role": "user", "content": "\n\n".join(f"[{i}] {w}" for i, w in enumerate(windows, 1))}],
                                       NMCK_SCHEMA)) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"knowledge_store: метод НМЦК не определён моделью: {type(e).__name__}: {e}")
        return None
    name = parsed.get("method")
    quote, filename = _quote_source(documents, parsed.get("quote"))
    for code, label in NMCK_METHOD_CODES.values():
        if label == name and quote:
            return {"value": code, "method": label, "quote": quote, "document": filename, "source": "llm_extraction"}
    return None


def style_chunks(text: str, chunk: int = CHUNK, limit: int = MAX_CHUNKS) -> List[str]:
    """Части документа для просмотра: до ``limit`` кусков по ``chunk`` символов, равномерно по тексту."""
    text = text or ""
    if len(text) <= chunk:
        return [text] if text.strip() else []
    count = min(limit, -(-len(text) // chunk))
    step = (len(text) - chunk) / max(count - 1, 1)
    return [text[int(i * step):int(i * step) + chunk] for i in range(count)]


async def assess_style(documents: Dict[int, dict], llm_call: LlmCall, concurrency: int = 4) -> Dict[str, Any]:
    """Оценивает единый стиль и отсутствие логических ошибок по документам.

    Args:
        documents: Документы экспертизы с текстами (``filename``, ``text``).
        llm_call: Функция вызова LLM.
        concurrency: Сколько частей проверять одновременно.

    Returns:
        dict: ``{"defects": [{"document", "quote", "issue"}], "checked_docs", "checked_parts", "errors"}``.
        Дефект входит в список только если его цитата подтверждена текстом части документа.
    """
    docs = sorted((d for d in documents.values() if (d.get("text") or "").strip()),
                  key=lambda d: (d.get("doc_code") != "docIzvejenieFiles", -len(d.get("text") or "")))[:MAX_DOCS]
    semaphore, errors = asyncio.Semaphore(concurrency), []

    async def check(doc: dict, part: str) -> List[dict]:
        """Один запрос по одной части документа."""
        try:
            async with semaphore:
                raw = await llm_call([{"role": "system", "content": STYLE_PROMPT},
                                      {"role": "user", "content": f"Документ «{doc.get('filename')}»:\n{part}"}], STYLE_SCHEMA)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}"[:200])
            return []
        out = []
        for item in (_parse(raw) or {}).get("defects") or []:
            if not isinstance(item, dict):
                continue
            found = facts_mod.find_quote(str(item.get("quote") or ""), part)
            if found and str(item.get("issue") or "").strip():
                out.append({"document": doc.get("filename"), "quote": found[:300], "issue": str(item["issue"]).strip()})
        return out

    jobs = [check(d, part) for d in docs for part in style_chunks(d["text"])]
    results = await asyncio.gather(*jobs)
    defects = [x for r in results for x in r]
    seen, unique = set(), []
    for d in defects:
        key = re.sub(r"\W+", " ", d["issue"]).casefold()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return {"defects": unique, "checked_docs": len(docs), "checked_parts": len(jobs), "errors": errors}


async def compute_assessed(fields: Sequence[dict], documents: Dict[int, dict],
                           llm_call: Optional[LlmCall]) -> Dict[str, Tuple[Any, dict]]:
    """Значения ``field2_2_2_0`` и ``field2_2_1_6`` (если они есть в форме) для передачи в :func:`summary.assemble`.

    Значения считаются до сборки, чтобы итоги подразделов и пояснения ``*_text`` учитывали их.

    Args:
        fields: Поля формы.
        documents: Документы экспертизы с текстами.
        llm_call: Функция вызова LLM или ``None``.

    Returns:
        dict: ``ключ → (значение, запись trace)``; ключ отсутствует, если определить значение не удалось.
    """
    keys = {f["field_key"] for f in fields}
    preset: Dict[str, Tuple[Any, dict]] = {}
    if NMCK_CHOICE_KEY in keys:
        found = await detect_nmck_choice(documents, llm_call)
        if found:
            preset[NMCK_CHOICE_KEY] = (found["value"], {
                "status": "verified" if found["source"] == "text_rule" else "proposed",
                "source": found["source"], "document": found.get("document"), "quote": found["quote"],
                "comment": f"Применён {found['method']} метод обоснования НМЦК"})
    if STYLE_KEY in keys and llm_call is not None:
        result = await assess_style(documents, llm_call)
        if result["checked_parts"] and len(result["errors"]) < result["checked_parts"]:
            defects = result["defects"][:3]
            if defects:
                entry = {"status": "proposed", "source": "llm_assessment", "quote": defects[0]["quote"],
                         "comment": " ".join(f"В документе «{d['document']}»: {d['issue'].rstrip('.')}." for d in defects),
                         "defects": defects}
                preset[STYLE_KEY] = (0, entry)
            else:
                entry = {"status": "proposed", "source": "llm_assessment", "quote": None,
                         "comment": "Единый стиль документации выдержан, логических ошибок и противоречий не выявлено "
                                    f"(просмотрено документов: {result['checked_docs']}, частей: {result['checked_parts']}).",
                         "note": "оценка модели без цитаты: требует проверки экспертом"}
                preset[STYLE_KEY] = (1, entry)
            if result["errors"]:
                entry["errors"] = result["errors"][:3]
    return preset

"""Факты для критериев формы: из XML извещения ЕИС и из текстов документов (RAG + LLM).

Конвейер для одной экспертизы (:func:`extract_facts`):

1. Поля раздела 1, которые решаются по структуре XML извещения, берутся из ``pe_documents.extraction``
   (результат :mod:`knowledge_store.eis_notice`, источник ``eis_xml``).
2. Для остальных полей типов ``presence``/``compliance`` :class:`FactExtractor` находит в
   ``pe_chunks`` релевантные фрагменты (pgvector), спрашивает LLM (``guided_json``) и **проверяет
   цитату**: она должна присутствовать в одном из показанных фрагментов. Ответ «1» без подтверждённой
   цитаты сохраняется как неподтверждённый и попадает в заключение как предложение модели (источник
   ``fact_extractor``); ответ «3» («определить нельзя») оставляет критерий эксперту.

Тексты документов повторно не читаются и не распознаются — используются только сохранённые чанки.
Клиенты LLM и эмбеддингов передаются снаружи, поэтому модуль тестируется без сети.
"""
import asyncio
import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from knowledge_store import eis_notice, repository as repo

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "fact_extractor_prompt.txt"
SOURCE_EIS = "eis_xml"
SOURCE_LLM = "fact_extractor"
LLM_KINDS = ("presence", "compliance")

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        # порядок свойств важен: модель сначала находит фрагмент и цитату, потом объясняет и лишь затем решает
        "fragment": {"type": "integer", "minimum": 0},
        "quote": {"type": "string", "maxLength": 400},
        "comment": {"type": "string", "maxLength": 500},
        "value": {"type": "integer", "enum": [0, 1, 2, 3]},
    },
    "required": ["fragment", "quote", "comment", "value"],
}

LlmCall = Callable[[List[dict], dict], Awaitable[str]]


@dataclass
class Fragment:
    """Фрагмент документа, показанный модели.

    Attributes:
        chunk_id: ``pe_chunks.id``.
        document_id: ``pe_documents.id``.
        page: Первая страница чанка (``None``, если страниц нет).
        text: Текст чанка.
        source: Подпись источника (имя файла/тип документа).
    """

    chunk_id: int
    document_id: int
    page: Optional[int]
    text: str
    source: str = ""


def normalize(text: str) -> str:
    """Нормализует текст для сравнения цитаты: регистр, пробелы, кавычки, тире, ё."""
    text = (text or "").casefold().replace("ё", "е")
    text = re.sub(r"[«»“”„\"'`]", "", text)
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    """Проверяет, что цитата дословно (с точностью до пробелов/регистра/кавычек) есть в тексте.

    Args:
        quote: Цитата из ответа модели.
        text: Текст фрагмента.

    Returns:
        bool: ``True``, если непустая цитата найдена в тексте.
    """
    needle = normalize(quote)
    return bool(needle) and needle in normalize(text)


def find_quote(quote: str, text: str, threshold: float = 0.88) -> Optional[str]:
    """Находит цитату в тексте: точно или с небольшими расхождениями (перенос слов, лишние символы OCR).

    Модели часто возвращают цитату с мелкими искажениями. Поэтому после точного поиска (с учётом
    регистра, пробелов, кавычек) ищется ближайшее окно слов текста, похожее на цитату не менее чем на
    ``threshold``. Возвращается фрагмент **исходного текста** — его последующая точная проверка по
    полному тексту документа проходит.

    Args:
        quote: Цитата из ответа модели.
        text: Текст фрагмента.
        threshold: Минимальное сходство (0..1) для нечёткого совпадения.

    Returns:
        Optional[str]: Подтверждённая цитата (текст из документа) или ``None``.
    """
    needle = normalize(quote)
    if not needle:
        return None
    if needle in normalize(text):
        return quote.strip()
    words = [(m.start(), m.end(), normalize(m.group())) for m in re.finditer(r"\S+", text or "")]
    words = [w for w in words if w[2]]
    n = len(needle.split())
    best_ratio, best_span = 0.0, None
    for size in range(max(1, n - 2), n + 3):
        for i in range(0, len(words) - size + 1):
            candidate = " ".join(w[2] for w in words[i:i + size])
            matcher = SequenceMatcher(None, candidate, needle, autojunk=False)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best_ratio, best_span = ratio, (i, i + size)
    if best_span and best_ratio >= threshold:
        return text[words[best_span[0]][0]:words[best_span[1] - 1][1]]
    return None


def criterion_text(label: str) -> str:
    """Убирает номер критерия из названия (``1.18. Наличие…`` → ``Наличие…``) — это поисковый запрос."""
    return re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", label or "").strip()


def build_messages(label: str, kind: str, fragments: Sequence[Fragment], system_prompt: str) -> List[dict]:
    """Собирает сообщения для LLM.

    Args:
        label: Название критерия.
        kind: Тип поля (``presence`` / ``compliance``).
        fragments: Фрагменты, найденные поиском.
        system_prompt: Текст системного промпта.

    Returns:
        list: Сообщения в формате chat completions.
    """
    shown = "\n\n".join(
        f"[Фрагмент {i}] ({f.source}{', стр. ' + str(f.page) if f.page else ''})\n{f.text}"
        for i, f in enumerate(fragments, 1))
    kind_hint = "наличие информации" if kind == "presence" else "соответствие требованиям законодательства"
    user = f"Критерий ({kind_hint}): {label}\n\nФрагменты документов:\n\n{shown}"
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


def parse_answer(raw: str) -> Optional[dict]:
    """Разбирает ответ модели в словарь; при невалидном JSON пытается починить через ``json_repair``."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        try:
            from json_repair import repair_json
            data = json.loads(repair_json(raw))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def validate_answer(answer: Optional[dict], fragments: Sequence[Fragment]) -> Optional[dict]:
    """Проверяет ответ модели и превращает его в факт.

    Значение должно быть 0/1/2/3 (3 — «по найденным фрагментам определить нельзя»: критерий остаётся
    эксперту). Цитата ищется в показанных фрагментах (сначала в указанном моделью,
    затем в остальных, с допуском на мелкие искажения — :func:`find_quote`). Признак ``verified``
    означает, что цитата подтверждена текстом. Ответ «1» без подтверждённой цитаты **не отбрасывается**,
    а сохраняется как неподтверждённый (``verified=False``) вместе с исходной цитатой модели и документом
    указанного ею фрагмента: в заключение он попадёт как предложение модели (``proposed``). Ответы «0» и «2»
    цитаты не требуют (отсутствие нечем процитировать).

    Args:
        answer: Разобранный ответ модели.
        fragments: Показанные модели фрагменты.

    Returns:
        Optional[dict]: ``{"value", "verified", "comment", "document_id", "page", "quote", "model_quote",
        "fragment"}`` или ``None``, если ответ невалиден (нет значения 0/1/2/3).
    """
    if not answer:
        return None
    try:
        value = int(answer.get("value"))
    except (TypeError, ValueError):
        return None
    if value not in (0, 1, 2, 3):
        return None
    quote = (answer.get("quote") or "").strip()
    comment = (answer.get("comment") or "").strip() or None
    found, found_text, cited = None, None, None
    try:
        idx = int(answer.get("fragment") or 0)
    except (TypeError, ValueError):
        idx = 0
    if 1 <= idx <= len(fragments):
        cited = fragments[idx - 1]
    if quote:
        order = ([cited] if cited else []) + [f for f in fragments if f is not cited]
        for fragment in order:
            found_text = find_quote(quote, fragment.text)
            if found_text:
                found = fragment
                break
    source = found or cited      # документ указанного моделью фрагмента, даже если цитату подтвердить не удалось
    return {"value": value, "verified": bool(found), "comment": comment,
            "document_id": source.document_id if source else None,
            "page": source.page if source else None, "quote": found_text[:500] if found else None,
            "model_quote": quote[:500] or None, "fragment": idx or None}


class FactExtractor:
    """Извлекает значения критериев формы из сохранённых текстов документов (RAG + LLM)."""

    def __init__(self, llm_call: LlmCall, embedder: Any, search: Optional[Callable] = None,
                 top_k: Optional[int] = None, concurrency: int = 4, system_prompt: Optional[str] = None,
                 recheck_k: Optional[int] = None, notice_search: Optional[Callable] = None, notice_k: int = 5):
        """Создаёт экстрактор.

        Args:
            llm_call: Асинхронная функция ``(messages, json_schema) -> str`` (ответ модели).
            embedder: Клиент эмбеддингов с методом ``get_embeddings(texts)``.
            search: Функция поиска ``(conn, expertise_id, vector, k) -> list[Fragment]``;
                по умолчанию — :func:`search_fragments` (pgvector).
            top_k: Сколько фрагментов показывать модели (по умолчанию ``PE_FACTS_TOP_K``, 8).
            concurrency: Максимум одновременных запросов к LLM.
            system_prompt: Текст промпта; по умолчанию читается ``prompts/fact_extractor_prompt.txt``.
            recheck_k: Если модель ответила «0» или «3» без цитаты, критерий проверяется повторно по такому числу
                фрагментов (по умолчанию ``PE_FACTS_RECHECK_K``, 16; ``0`` — без повторной проверки):
                «не нашли» среди первых фрагментов ещё не значит «отсутствует».
            notice_search: Поиск только по документам извещения (``(conn, expertise_id, vector, k)``); для
                критериев раздела 1 («Наличие информации…» об извещении) его фрагменты ставятся первыми.
                По умолчанию включён только вместе со стандартным поиском pgvector.
            notice_k: Сколько фрагментов извещения добавлять.
        """
        self.llm_call = llm_call
        self.embedder = embedder
        self.search = search or search_fragments
        self.top_k = top_k or int(os.getenv("PE_FACTS_TOP_K", "8"))
        self.recheck_k = int(os.getenv("PE_FACTS_RECHECK_K", "16")) if recheck_k is None else recheck_k
        self.notice_search = notice_search or (search_notice_fragments if search is None else None)
        self.notice_k = notice_k
        self.errors: Dict[str, str] = {}
        self.semaphore = asyncio.Semaphore(concurrency)
        self.system_prompt = system_prompt or PROMPT_PATH.read_text(encoding="utf-8")

    async def extract_field(self, conn, expertise_id: int, field: dict) -> Optional[dict]:
        """Определяет значение одного критерия.

        Args:
            conn: Соединение ``asyncpg``.
            expertise_id: ID экспертизы.
            field: Поле формы (``field_key``, ``label``, ``value_kind``).

        Returns:
            Optional[dict]: Факт (см. :func:`validate_answer`) с ``fact_key`` или ``None``, если нет
            текстов для поиска либо ответ модели не принят.
        """
        query = criterion_text(field["label"])
        vectors = await self.embedder.get_embeddings([query])
        if vectors is None or len(vectors) == 0:
            self.errors[field["field_key"]] = "эмбеддинг запроса не получен"
            return None
        fragments = await self.search(conn, expertise_id, vectors[0], self.top_k)
        if self.notice_search and re.match(r"\s*1\.\d", field["label"] or ""):
            notice = await self.notice_search(conn, expertise_id, vectors[0], self.notice_k)
            ids = {f.chunk_id for f in notice}
            fragments = list(notice) + [f for f in fragments if f.chunk_id not in ids]
        if not fragments:
            self.errors[field["field_key"]] = "в базе нет фрагментов для поиска"
            return None
        fact = await self._ask(field, fragments)
        if fact and fact["value"] in (0, 3) and not fact["verified"] and self.recheck_k > len(fragments):
            wider = await self.search(conn, expertise_id, vectors[0], self.recheck_k)
            if len(wider) > len(fragments):
                again = await self._ask(field, wider)
                fact = again or fact
        if fact:
            fact["fact_key"] = field["field_key"]
        return fact

    async def _ask(self, field: dict, fragments: Sequence[Fragment]) -> Optional[dict]:
        """Один запрос к модели по фрагментам; возвращает принятый факт или ``None``."""
        messages = build_messages(field["label"], field["value_kind"], fragments, self.system_prompt)
        for _ in range(2):      # обрезанный/невалидный ответ — одна повторная попытка
            async with self.semaphore:
                raw = await self.llm_call(messages, ANSWER_SCHEMA)
            fact = validate_answer(parse_answer(raw), fragments)
            if fact:
                return fact
            self.errors[field["field_key"]] = f"невалидный ответ модели: {str(raw)[:300]}"
        return None


async def search_notice_fragments(conn, expertise_id: int, vector, k: int) -> List[Fragment]:
    """Ищет ближайшие чанки только среди документов извещения (``docIzvejenieFiles``, ``linkDocs``).

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        vector: Вектор запроса.
        k: Сколько фрагментов вернуть.

    Returns:
        list[Fragment]: Фрагменты по возрастанию расстояния.
    """
    rows = await conn.fetch(repo.SQL_SEARCH_NOTICE_CHUNKS, int(expertise_id), repo.vector_literal(vector), int(k),
                            ["docIzvejenieFiles", "linkDocs"])
    return [Fragment(r["chunk_id"], r["document_id"], r["page_from"], r["text"],
                     r["filename"] or r["doc_code"] or "") for r in rows]


async def search_fragments(conn, expertise_id: int, vector, k: int) -> List[Fragment]:
    """Ищет ближайшие чанки экспертизы по косинусному расстоянию (pgvector).

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        vector: Вектор запроса.
        k: Сколько фрагментов вернуть.

    Returns:
        list[Fragment]: Фрагменты по возрастанию расстояния.
    """
    rows = await conn.fetch(repo.SQL_SEARCH_CHUNKS, int(expertise_id), repo.vector_literal(vector), int(k))
    return [Fragment(r["chunk_id"], r["document_id"], r["page_from"], r["text"],
                     r["filename"] or r["doc_code"] or "") for r in rows]


def build_eis_facts(notices: Sequence[dict], fields: Sequence[dict]) -> List[dict]:
    """Формирует факты раздела 1 из результатов разбора XML.

    Args:
        notices: Записи :func:`knowledge_store.repository.get_eis_notices`.
        fields: Поля формы.

    Returns:
        list[dict]: Факты источника ``eis_xml``. Если версий извещения несколько — берётся последняя.
    """
    best = None
    for item in notices:
        data = item["eis_notice"]
        data = json.loads(data) if isinstance(data, str) else data
        if data and (best is None or data.get("version", 0) >= best[1].get("version", 0)):
            best = (item["id"], data)
    if not best:
        return []
    doc_id, data = best
    return [{"fact_key": key, "value": {"value": f.value, "verified": True, "comment": None},
             "document_id": doc_id, "page": None, "quote": f.evidence, "confidence": 1.0}
            for key, f in eis_notice.map_to_fields(data, fields).items()]


async def extract_facts(conn, expertise_id: int, form_code: str, extractor: Optional[FactExtractor]) -> Dict[str, int]:
    """Строит и сохраняет факты экспертизы по полям формы.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        form_code: Код формы из справочника (``44fz_competition_obj6``).
        extractor: :class:`FactExtractor`; ``None`` — только факты из XML.

    Returns:
        dict: Счётчики: ``eis`` (из XML), ``llm`` (принятые ответы модели), ``rejected`` (отвергнутые
        или без текста), ``fields`` (полей проверялось).
    """
    fields = [dict(r) for r in await repo.get_form_fields(conn, form_code)]
    eis_facts = build_eis_facts(await repo.get_eis_notices(conn, expertise_id), fields)
    await repo.replace_facts(conn, expertise_id, SOURCE_EIS, eis_facts)
    stats = {"eis": len(eis_facts), "llm": 0, "rejected": 0, "fields": 0}
    if extractor is None:
        return stats
    solved = {f["fact_key"] for f in eis_facts}
    todo = [f for f in fields if f["value_kind"] in LLM_KINDS and f["field_key"] not in solved]
    stats["fields"] = len(todo)
    results = await asyncio.gather(*(extractor.extract_field(conn, expertise_id, f) for f in todo),
                                   return_exceptions=True)
    llm_facts = []
    for field, res in zip(todo, results):
        if not isinstance(res, dict):
            # причина отказа сохраняется фактом без значения — видна в trace, а не теряется
            reason = (f"{type(res).__name__}: {res}"[:300] if isinstance(res, Exception)
                      else extractor.errors.get(field["field_key"], "ответ не получен"))
            llm_facts.append({"fact_key": field["field_key"],
                              "value": {"value": None, "verified": False, "comment": None, "error": reason},
                              "document_id": None, "page": None, "quote": None, "confidence": 0.0})
            stats["rejected"] += 1
            continue
        if isinstance(res, dict):
            llm_facts.append({"fact_key": res["fact_key"],
                              "value": {"value": res["value"], "verified": res["verified"], "comment": res["comment"],
                                        "model_quote": res.get("model_quote"), "fragment": res.get("fragment")},
                              "document_id": res["document_id"], "page": res["page"], "quote": res["quote"],
                              "confidence": 0.8 if res["verified"] else 0.3})
    await repo.replace_facts(conn, expertise_id, SOURCE_LLM, llm_facts)
    stats["llm"] = len(llm_facts) - stats["rejected"]
    return stats


def make_llm_call(client: Any, model: str, rate: float = 1.5, max_tokens: int = 1600) -> LlmCall:
    """Создаёт функцию вызова LLM для :class:`FactExtractor` поверх OpenAI-совместимого клиента.

    Использует ``guided_json`` (как ``TypeDataExtractor``) и общий ограничитель частоты запросов.

    Args:
        client: ``AsyncOpenAI``-совместимый клиент (``configs.llm_client.get_llm()[0]``).
        model: Имя модели.
        rate: Максимум запросов в секунду.
        max_tokens: Лимит токенов ответа.

    Returns:
        Функция ``(messages, schema) -> str``.
    """
    from configs.rate_limiter import TokenBucket

    bucket = TokenBucket(rate=rate)

    async def call(messages: List[dict], schema: dict) -> str:
        """Один запрос к модели с ограничением частоты; возвращает текст ответа."""
        await bucket.acquire()
        response = await client.chat.completions.create(
            model=model, messages=messages, extra_body={"guided_json": schema},
            max_tokens=max_tokens, temperature=0.1)
        return (response.choices[0].message.content or "").strip()

    return call

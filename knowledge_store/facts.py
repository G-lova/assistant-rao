"""Факты для критериев формы: из XML извещения ЕИС и из текстов документов (RAG + LLM).

Конвейер для одной экспертизы (:func:`extract_facts`):

1. Поля раздела 1, которые решаются по структуре XML извещения, берутся из ``pe_documents.extraction``
   (результат :mod:`knowledge_store.eis_notice`, источник ``eis_xml``).
2. Для остальных полей типов ``presence``/``compliance`` :class:`FactExtractor` находит в
   ``pe_chunks`` релевантные фрагменты (pgvector), спрашивает LLM (``guided_json``) и **проверяет
   цитату**: она должна дословно присутствовать в одном из показанных фрагментов. Ответ «1» без
   подтверждённой цитаты не принимается (источник ``fact_extractor``).

Тексты документов повторно не читаются и не распознаются — используются только сохранённые чанки.
Клиенты LLM и эмбеддингов передаются снаружи, поэтому модуль тестируется без сети.
"""
import asyncio
import json
import re
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
        "value": {"type": "integer", "enum": [0, 1, 2]},
        "fragment": {"type": "integer", "minimum": 0},
        "quote": {"type": "string", "maxLength": 500},
        "comment": {"type": "string", "maxLength": 600},
    },
    "required": ["value", "fragment", "quote", "comment"],
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

    Правила: значение должно быть 0/1/2; для ``1`` цитата обязана быть найдена в показанных фрагментах
    (сначала в указанном моделью, затем в остальных); ответ ``1`` без подтверждённой цитаты отвергается.

    Args:
        answer: Разобранный ответ модели.
        fragments: Показанные модели фрагменты.

    Returns:
        Optional[dict]: ``{"value", "verified", "comment", "document_id", "page", "quote"}`` или ``None``,
        если ответ не принят.
    """
    if not answer:
        return None
    try:
        value = int(answer.get("value"))
    except (TypeError, ValueError):
        return None
    if value not in (0, 1, 2):
        return None
    quote = (answer.get("quote") or "").strip()
    comment = (answer.get("comment") or "").strip() or None
    found = None
    if quote:
        order = []
        try:
            idx = int(answer.get("fragment") or 0)
            if 1 <= idx <= len(fragments):
                order.append(fragments[idx - 1])
        except (TypeError, ValueError):
            pass
        order += [f for f in fragments if f not in order]
        found = next((f for f in order if quote_in_text(quote, f.text)), None)
    if value == 1 and not found:
        return None  # утверждение «в наличии/соответствует» без доказательства не принимаем
    return {"value": value, "verified": bool(found), "comment": comment,
            "document_id": found.document_id if found else None,
            "page": found.page if found else None, "quote": quote[:500] if found else None}


class FactExtractor:
    """Извлекает значения критериев формы из сохранённых текстов документов (RAG + LLM)."""

    def __init__(self, llm_call: LlmCall, embedder: Any, search: Optional[Callable] = None,
                 top_k: int = 6, concurrency: int = 4, system_prompt: Optional[str] = None):
        """Создаёт экстрактор.

        Args:
            llm_call: Асинхронная функция ``(messages, json_schema) -> str`` (ответ модели).
            embedder: Клиент эмбеддингов с методом ``get_embeddings(texts)``.
            search: Функция поиска ``(conn, expertise_id, vector, k) -> list[Fragment]``;
                по умолчанию — :func:`search_fragments` (pgvector).
            top_k: Сколько фрагментов показывать модели.
            concurrency: Максимум одновременных запросов к LLM.
            system_prompt: Текст промпта; по умолчанию читается ``prompts/fact_extractor_prompt.txt``.
        """
        self.llm_call = llm_call
        self.embedder = embedder
        self.search = search or search_fragments
        self.top_k = top_k
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
            return None
        fragments = await self.search(conn, expertise_id, vectors[0], self.top_k)
        if not fragments:
            return None
        messages = build_messages(field["label"], field["value_kind"], fragments, self.system_prompt)
        async with self.semaphore:
            raw = await self.llm_call(messages, ANSWER_SCHEMA)
        fact = validate_answer(parse_answer(raw), fragments)
        if fact:
            fact["fact_key"] = field["field_key"]
        return fact


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
    for res in results:
        if isinstance(res, dict):
            llm_facts.append({"fact_key": res["fact_key"],
                              "value": {"value": res["value"], "verified": res["verified"], "comment": res["comment"]},
                              "document_id": res["document_id"], "page": res["page"], "quote": res["quote"],
                              "confidence": 0.8 if res["verified"] else 0.3})
        else:
            stats["rejected"] += 1
    await repo.replace_facts(conn, expertise_id, SOURCE_LLM, llm_facts)
    stats["llm"] = len(llm_facts)
    return stats


def make_llm_call(client: Any, model: str, rate: float = 1.5, max_tokens: int = 800) -> LlmCall:
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

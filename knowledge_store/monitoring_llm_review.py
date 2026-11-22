"""ИИ-слой проверки критериев мониторинга закупок для нерегулярных случаев (правила воздержались или нужны причины).

Правила (:mod:`knowledge_store.monitoring_nmck`, :mod:`knowledge_store.monitoring_object`) ловят типовые нарушения. Для остального
(формальные источники цены, реквизиты запросов КП, противоречия ТЗ и КП, избыточные требования) модель читает документы закупки и
возвращает нарушения с дословной цитатой. **Цитата проверяется: замечание, цитаты которого нет ни в одном документе, отбрасывается** —
по образцу :mod:`knowledge_store.primary_llm_review`. Вердикт «Соответствует» модель не выносит: пустой результат означает
«нарушений с цитатой не найдено», итоговый вердикт собирает вызывающий код вместе с правилами.

Модель вызывается через ``risk_monitoring.llm_json.LLMJson`` (``await llm.ask(system, user, schema, ...)``).
"""
import re
from typing import Any, Dict, List, Optional

# критерий → что считается нарушением (из формулировок итоговых заключений)
CRITERIA: Dict[str, str] = {
    "NMCK": ("Обоснование НМЦК/цены договора: нет документа обоснования; источники цены формальны (КП без реквизитов запроса, "
             "дат, исходящих номеров; менее трёх сопоставимых источников); совмещены методы, не предусмотренные методикой "
             "(Приказ Росгвардии № 45, Положение о закупке); цена в КП не соответствует предмету закупки."),
    "OBJECT": ("Описание объекта закупки (ТЗ): избыточные или неправомерные требования (например, «автомобили с логотипом, "
               "согласованным в ГУ МВД»); ТЗ противоречит КП или договору; нет срока и порядка приёмки или оплаты; нет требования "
               "о лицензии на охранную деятельность; не указаны объём, периодичность, места оказания услуг."),
}
MAX_TEXT = 12000        # символов документов, которые видит модель
MAX_FINDINGS = 5
MIN_QUOTE = 12          # минимальная длина цитаты
_LEAK_RE = re.compile(r"давайте|пересмотрим|попробуем|на самом деле", re.I)

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array", "maxItems": MAX_FINDINGS, "items": {
    "type": "object", "properties": {
        "document": {"type": "string", "maxLength": 200},
        "quote": {"type": "string", "maxLength": 300},
        "explanation": {"type": "string", "maxLength": 300}},
    "required": ["document", "quote", "explanation"], "additionalProperties": False}}},
    "required": ["findings"], "additionalProperties": False}


def system_prompt(criterion: str) -> str:
    """Системный промпт для критерия ``criterion`` (ключ :data:`CRITERIA`)."""
    return ("Ты проверяешь документы закупки на соответствие критерию мониторинга. Критерий — " + CRITERIA[criterion]
            + "\nПравила: сообщай только нарушение, которое подтверждается дословной цитатой из документов закупки (поле quote — "
              "подряд идущие слова документа, до 300 символов; document — имя файла из заголовка «=== файл»). Не выноси вердикт "
              "«соответствует» и не придумывай нарушения: если нарушений с цитатой нет, верни пустой список findings. Не "
              "ссылайся на нормы, которых нет в тексте документов. Пояснение — одно-два предложения без рассуждений. Ответ — "
              'строго JSON {"findings": [{"document": "...", "quote": "...", "explanation": "..."}]}.')


def _norm(text: str) -> str:
    """Нормализация для поиска цитаты: пробелы и регистр, кавычки одного вида."""
    return re.sub(r"\s+", " ", text.replace("«", '"').replace("»", '"')).strip().casefold()


def build_source(docs: Dict[str, str], limit: int = MAX_TEXT) -> str:
    """Склеивает документы закупки (имя → текст) в один текст с заголовками «=== файл», не длиннее ``limit``."""
    parts, left = [], limit
    for name, text in docs.items():
        chunk = f"=== файл: {name}\n{re.sub(r'[ \t]+', ' ', text or '').strip()}"[:max(left, 0)]
        if not chunk:
            break
        parts.append(chunk)
        left -= len(chunk)
    return "\n\n".join(parts)


def verify(findings: List[Any], source: str) -> List[Dict[str, str]]:
    """Оставляет замечания с цитатой, дословно присутствующей в ``source``; без дублей и следов рассуждений."""
    haystack, out, seen = _norm(source), [], set()
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        quote = _norm(str(f.get("quote") or ""))
        explanation = str(f.get("explanation") or "").strip()
        if len(quote) < MIN_QUOTE or quote not in haystack or quote in seen or _LEAK_RE.search(explanation):
            continue
        seen.add(quote)
        out.append({"document": str(f.get("document") or "").strip(), "quote": str(f["quote"]).strip(),
                    "explanation": explanation})
    return out[:MAX_FINDINGS]


async def review(llm: Any, criterion: str, docs: Dict[str, str], trace: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Нарушения по критерию, найденные моделью и подтверждённые цитатой.

    Args:
        llm: ``LLMJson`` (или объект с ``ask(system, user, schema, ...)``).
        criterion: Ключ :data:`CRITERIA` (``NMCK`` или ``OBJECT``).
        docs: Тексты документов закупки: имя файла → текст.
        trace: Если передан словарь — пишутся ``has_text``, ``raw`` (ответ модели) и ``verified`` (после проверки цитат).

    Returns:
        list[dict]: ``document``, ``quote``, ``explanation``; пусто, если текста нет или нарушений с цитатой не найдено.
    """
    source = build_source(docs)
    if trace is not None:
        trace.update({"has_text": bool(source), "raw": [], "verified": []})
    if not source:
        return []
    answer = await llm.ask(system_prompt(criterion), f"ДОКУМЕНТЫ ЗАКУПКИ:\n{source}", SCHEMA, max_tokens=1500,
                           temperature=0.1, list_key="findings")
    verified = verify(answer.get("findings") or [], source)
    if trace is not None:
        trace.update({"raw": list(answer.get("findings") or []), "verified": verified})
    return verified

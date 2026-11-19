"""Замечания к текстовой части заключения первичного эксперта, определяемые моделью: «Недостаточная аргументация»,
«Противоречия», «Формальные недостатки».

Кодом эти категории определяются только для объективных случаев (заглушки, «0» без замечаний — :mod:`knowledge_store.primary_review`).
Остальное — по смыслу текста, поэтому модель получает замечания и вывод первичного (``field3``, ``field4``, текстовые поля критериев
``*_text``) и вывод итогового заключения как опору, а возвращает найденные недостатки с дословной цитатой. **Цитата проверяется:
замечание без цитаты, которой нет в тексте первичного, отбрасывается** — так модель не может выдумать недостаток.

На оценку ``range`` эти замечания не влияют (связи с оценкой не показали ни ``issues``, ни ``remarks``): они дополняют комментарий и
``categories``. Модель вызывается через ``risk_monitoring.llm_json.LLMJson`` (``await llm.ask(system, user, schema)``).
"""
import re
from typing import Any, Dict, List, Optional

# код категории таксономии → описание для промпта
CATEGORY_HINTS: Dict[str, str] = {
    "ARGUMENTATION": ("Недостаточная аргументация: нарушение названо без пояснения, в чём оно состоит; нет ссылки на пункт документа "
                      "или норму; оценочные суждения вместо фактов («некорректно», «неудачно») без обоснования."),
    "CONTRADICTION": ("Противоречия: разделы заключения противоречат друг другу (замечание есть, а вывод «соответствует»; "
                      "вывод о нарушении без замечания; утверждение в одном месте опровергается в другом)."),
    "FORMAL": ("Формальные недостатки: ошибки оформления, терминологии и структуры (опечатки, неверные термины, "
               "нарушенная нумерация замечаний, перепутанные названия документов, не по форме)."),
}
MAX_TEXT = 6000            # символов текста первичного, который видит модель
MAX_FINDINGS = 6           # не больше замечаний на заключение
MAX_PER_CATEGORY = 2       # и не больше замечаний одной категории (однотипные цитаты повторяют одну мысль)
MIN_QUOTE = 8              # минимальная длина цитаты

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array", "maxItems": MAX_FINDINGS, "items": {
    "type": "object", "properties": {
        "category": {"type": "string", "enum": list(CATEGORY_HINTS)},
        "quote": {"type": "string", "maxLength": 300},
        "explanation": {"type": "string", "maxLength": 300}},
    "required": ["category", "quote", "explanation"], "additionalProperties": False}}},
    "required": ["findings"], "additionalProperties": False}

SYSTEM = ("Ты проверяешь заключение первичного эксперта по закупке или отчётным документам. Найди в его текстах недостатки трёх видов:\n"
          + "\n".join(f"- {code}: {text}" for code, text in CATEGORY_HINTS.items())
          + "\nПравила: сообщай только недостаток, который можно подтвердить дословной цитатой из заключения ПЕРВИЧНОГО эксперта "
            "(поле quote — подряд идущие слова из его текста, до 300 символов). Не оценивай, правильно ли эксперт нашёл нарушение — "
            "это проверяется отдельно. Слова «необоснованно», «некорректно», «не установил» в описании нарушения ЗАКАЗЧИКА — это содержание "
            "замечания, а не слабая аргументация эксперта: ARGUMENTATION ставь, только если замечание ни в одном месте не поясняет, в чём "
            "нарушение, и не ссылается ни на пункт документа, ни на норму; по одной сути недостатка — одно замечание, не повторяй "
            "однотипные цитаты. Если недостатков нет, верни пустой список findings. Не придумывай недостатки.")


def opinion_text(data: Dict[str, Any], limit: int = MAX_TEXT) -> str:
    """Текстовая часть заключения для модели: ``field3`` (замечания), ``field4`` (вывод), текстовые поля критериев ``*_text``."""
    parts = []
    for key in ("field3", "field4"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"[{key}] {value.strip()}")
    for key, value in data.items():
        if key.endswith("_text") and isinstance(value, str) and value.strip():
            parts.append(f"[{key}] {value.strip()}")
    return "\n".join(parts)[:limit]


def _norm(text: str) -> str:
    """Нормализация для поиска цитаты: пробелы и регистр, кавычки одного вида."""
    return re.sub(r"\s+", " ", text.replace("«", '"').replace("»", '"')).strip().casefold()


def verify(findings: List[Dict[str, Any]], source: str) -> List[Dict[str, str]]:
    """Оставляет замечания с известной категорией и цитатой, которая дословно есть в тексте первичного."""
    haystack = _norm(source)
    out, seen = [], set()
    for f in findings or []:
        code, quote = f.get("category"), _norm(str(f.get("quote") or ""))
        if code not in CATEGORY_HINTS or len(quote) < MIN_QUOTE or quote not in haystack or (code, quote) in seen:
            continue
        seen.add((code, quote))
        if sum(1 for o in out if o["category"] == code) >= MAX_PER_CATEGORY:
            continue
        out.append({"category": code, "quote": str(f["quote"]).strip(), "explanation": str(f.get("explanation") or "").strip()})
    return out[:MAX_FINDINGS]


async def review(llm: Any, primary: Dict[str, Any], summary: Optional[Dict[str, Any]] = None,
                 trace: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Недостатки текстовой части заключения первичного, найденные моделью и подтверждённые цитатой.

    Args:
        llm: ``LLMJson`` (или объект с ``ask(system, user, schema)``).
        primary: Данные заключения первичного эксперта.
        summary: Данные итогового заключения — только как опора (его вывод ``field4`` показывается модели).
        trace: Если передан словарь — в него пишется диагностика: ``has_text`` (был ли текст), ``raw`` (ответ модели без проверки),
            ``verified`` (после проверки цитат); нужна, чтобы отличить «модель ничего не нашла» от «цитаты не прошли проверку».

    Returns:
        list[dict]: ``category``, ``quote``, ``explanation``; пусто, если текста нет или недостатков не найдено.
    """
    source = opinion_text(primary)
    if trace is not None:
        trace.update({"has_text": bool(source), "raw": [], "verified": []})
    if not source:
        return []
    user = f"ЗАКЛЮЧЕНИЕ ПЕРВИЧНОГО ЭКСПЕРТА:\n{source}"
    reference = (summary or {}).get("field4")
    if isinstance(reference, str) and reference.strip():
        user += f"\n\nВЫВОД ИТОГОВОГО ЗАКЛЮЧЕНИЯ (для сравнения):\n{reference.strip()[:1500]}"
    answer = await llm.ask(SYSTEM, user, SCHEMA, max_tokens=1500, temperature=0.1, list_key="findings")
    verified = verify(answer.get("findings") or [], source)
    if trace is not None:
        trace.update({"raw": list(answer.get("findings") or []), "verified": verified})
    return verified

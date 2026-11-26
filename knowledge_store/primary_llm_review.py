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

from knowledge_store import primary_review

# код категории таксономии → описание для промпта
CATEGORY_HINTS: Dict[str, str] = {
    "ARGUMENTATION": ("Недостаточная аргументация: нарушение названо без пояснения, в чём оно состоит; нет ссылки на пункт документа "
                      "или норму; оценочные суждения вместо фактов («некорректно», «неудачно») без обоснования."),
    "CONTRADICTION": ("Противоречия: разделы заключения противоречат друг другу (замечание есть, а вывод «соответствует»; "
                      "вывод о нарушении без замечания; утверждение в одном месте опровергается в другом)."),
    "FORMAL": ("Формальные недостатки: ошибки оформления, терминологии и структуры (опечатки, неверные термины, "
               "нарушенная нумерация замечаний, перепутанные названия документов, не по форме). Не отмечай мелочи: двойные пробелы, "
               "отсутствие точки в конце, расстановку запятых."),
}
MAX_TEXT = 6000            # символов текста первичного, который видит модель
MAX_FINDINGS = 6           # не больше замечаний на заключение
MAX_PER_CATEGORY = 2       # и не больше замечаний одной категории (однотипные цитаты повторяют одну мысль)
MIN_QUOTE = 8              # минимальная длина цитаты
MIN_FIELD_TEXT = 40        # отчётные документы: минимальная длина текстового поля, чтобы его показать модели
# следы рассуждений модели в ответе («Давайте пересмотрим цитату»): такое замечание ненадёжно
_LEAK_RE = re.compile(r"давайте|пересмотрим|попробуем|на самом деле", re.I)

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array", "maxItems": MAX_FINDINGS, "items": {
    "type": "object", "properties": {
        "category": {"type": "string", "enum": list(CATEGORY_HINTS)},
        "quote": {"type": "string", "maxLength": 300},
        "explanation": {"type": "string", "maxLength": 300}},
    "required": ["category", "quote", "explanation"], "additionalProperties": False}}},
    "required": ["findings"], "additionalProperties": False}

SYSTEM = (
    "Ты проверяешь заключение первичного эксперта по закупке или отчётным документам. Найди в его текстах недостатки трёх видов:\n"
    + "\n".join(f"- {code}: {text}" for code, text in CATEGORY_HINTS.items())
    + "\nПравила: сообщай только недостаток, который можно подтвердить дословной цитатой из заключения ПЕРВИЧНОГО эксперта "
      "(поле quote — подряд идущие слова из его текста, до 300 символов). Не оценивай, правильно ли эксперт нашёл нарушение — "
      "это проверяется отдельно, и не отмечай расхождения с итоговым заключением как противоречия (итоговое — только для справки): "
      "CONTRADICTION — только противоречие внутри самого заключения первичного. Слова «необоснованно», «некорректно», «не установил» "
      "в описании нарушения ЗАКАЗЧИКА — это содержание замечания, а не слабая аргументация эксперта: ARGUMENTATION ставь, только если "
      "замечание ни в одном месте не поясняет, в чём нарушение, и не ссылается ни на пункт документа, ни на норму; по одной сути "
      "недостатка — одно замечание, не повторяй однотипные цитаты. Если недостатков нет, верни пустой список findings. "
      "Не придумывай недостатки. Ответ — строго JSON "
      '{"findings": [{"category": "...", "quote": "...", "explanation": "..."}]}; пояснение — одно-два предложения без рассуждений, '
      'без служебных обозначений полей в квадратных скобках (пиши «в перечне документов», а не «[field1_1]»).')


# Поля отчётных форм, которые не проверяются моделью: перечень документов подтягивается из портала (ссылки ЕИС, имена файлов),
# замечания к нему были бы замечаниями к исходным данным, а не к работе эксперта.
LLM_SKIP_FIELDS = ("field1_1",)


_FIELD_REF = re.compile(r"\s*\(?(?:в поле |поле |полях )?\[field\w*\]\)?")


def _strip_field_refs(text: str) -> str:
    """Убирает служебные ссылки на ключи полей (``[field1_1]``, «поле [field5_0_0]»): заключение читает эксперт, не разработчик."""
    return re.sub(r"\s{2,}", " ", _FIELD_REF.sub("", text)).strip()


def _clip(text: str, limit: int) -> str:
    """Обрезает текст по границе слова и добавляет «…», а не рвёт фразу на середине."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def opinion_text(data: Dict[str, Any], limit: int = MAX_TEXT, report: bool = False) -> str:
    """Текстовая часть заключения для модели.

    Закупки: ``field3`` (замечания), ``field4`` (вывод), текстовые поля критериев ``*_text``. Отчётные документы: замечания лежат
    в других полях (перечень документов ``field1_1``, предмет ``field2_1``, комментарии и выводы по разделам ``field5_*``,
    ``field6_*``), поэтому берутся все текстовые поля формы длиной от :data:`MIN_FIELD_TEXT` символов.

    Args:
        data: Содержимое заключения.
        limit: Максимум символов.
        report: Заключение по отчётным документам.
    """
    parts = []
    for key in ("field3", "field4"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"[{key}] {value.strip()}")
    for key, value in data.items():
        if key.endswith("_text") and isinstance(value, str) and value.strip():
            parts.append(f"[{key}] {value.strip()}")
    if report:
        for key in sorted(data):
            value = data[key]
            if (key.startswith("field") and key not in ("field3", "field4") + LLM_SKIP_FIELDS and not key.endswith("_text")
                    and isinstance(value, str) and len(value.strip()) >= MIN_FIELD_TEXT):
                parts.append(f"[{key}] {value.strip()}")
    return "\n".join(parts)[:limit]


def _norm(text: str) -> str:
    """Нормализация для поиска цитаты: пробелы и регистр, кавычки одного вида."""
    return re.sub(r"\s+", " ", text.replace("«", '"').replace("»", '"')).strip().casefold()


def verify(findings: List[Any], source: str) -> List[Dict[str, str]]:
    """Оставляет замечания с известной категорией и цитатой, которая дословно есть в тексте первичного.

    Модель не всегда следует схеме: ключи ``type`` / ``description`` принимаются как ``category`` / ``explanation``; элементы, не
    являющиеся объектами, и замечания со следами рассуждений в пояснении отбрасываются.
    """
    haystack = _norm(source)
    out, seen = [], set()
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        code = f.get("category") or f.get("type")
        quote = _norm(str(f.get("quote") or ""))
        explanation = str(f.get("explanation") or f.get("description") or "").strip()
        if (code not in CATEGORY_HINTS or len(quote) < MIN_QUOTE or quote not in haystack or (code, quote) in seen
                or _LEAK_RE.search(explanation)):
            continue
        seen.add((code, quote))
        if sum(1 for o in out if o["category"] == code) >= MAX_PER_CATEGORY:
            continue
        out.append({"category": code, "quote": str(f["quote"]).strip(), "explanation": explanation})
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
    source = opinion_text(primary, report=primary_review.is_report_data(summary or primary))
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


def to_issues(findings: List[Dict[str, str]], categories: Optional[Any] = None) -> List[primary_review.Issue]:
    """Замечания модели → дефекты для комментария и категорий (:class:`primary_review.Issue`, код ``LLM_<категория>``).

    Args:
        findings: Результат :func:`review`.
        categories: Какие категории оставить (``settings.PRIMARY_REVIEW_LLM_CATEGORIES``); ``None`` — все.
    """
    return [primary_review.Issue(f"LLM_{f['category']}", "minor", f"«{_clip(f['quote'], 80)}»: {_clip(_strip_field_refs(f['explanation']), 200)}", [])
            for f in findings if categories is None or f["category"] in categories]

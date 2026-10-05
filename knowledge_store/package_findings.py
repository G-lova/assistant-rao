"""Выводы о комплекте документов на базе фактов («РАО Эксперт»).

Превращает факты экспертизы (``pe_facts``) и справочник полей формы в компактный дайджест:

* счётчики (сколько критериев проверено, в наличии, отсутствует, не предусмотрено, подтверждено цитатой);
* список критериев со значением «отсутствует/не соответствует» с доказательствами;
* список непроверенных ответов (без цитаты) — их нельзя выдавать за установленный факт.

Дайджест используется дважды: как вход для финальной проверки согласованности
(:class:`evaluate_documents.consistency_checker.ConsistencyChecker`) и как блок ``facts_summary``
в ответе ``/evaluate-documents``. Модуль не зависит от БД и сети.
"""
import json
import re
from typing import Any, Dict, List, Sequence

MAX_QUOTE = 200
MAX_ITEMS_FOR_LLM = 25
PRESENCE_WORDS = {0: "отсутствует", 1: "в наличии", 2: "не предусмотрено"}
COMPLIANCE_WORDS = {0: "не соответствует", 1: "соответствует", 2: "не применимо"}


def _as_dict(value: Any) -> dict:
    """Приводит JSONB-значение факта к словарю (строка JSON, словарь или голое число)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return {"value": value, "verified": False, "comment": None}
    return {}


def short_label(label: str) -> str:
    """Название критерия без хвостовых пояснений в скобках и лишних пробелов (для вывода)."""
    return re.sub(r"\s+", " ", label or "").strip()


def build_digest(facts: Sequence[dict], fields: Sequence[dict], form_code: str) -> Dict[str, Any]:
    """Строит дайджест фактов по форме заключения.

    Args:
        facts: Записи ``pe_facts`` (``fact_key``, ``value``, ``page``, ``quote``, ``source``).
        fields: Поля формы (``field_key``, ``label``, ``value_kind``, ``section``).
        form_code: Код формы заключения.

    Returns:
        dict: ``{"form", "stats", "missing", "unverified"}``. В ``missing`` — критерии со значением 0
        (поля ``field_key``, ``criterion``, ``kind``, ``verdict``, ``comment``, ``quote``, ``page``,
        ``source``); в ``unverified`` — ответы LLM без подтверждающей цитаты.
    """
    by_key = {f["field_key"]: f for f in fields}
    stats = {"checked": 0, "present": 0, "missing": 0, "not_provided": 0, "verified": 0,
             "from_xml": 0, "from_llm": 0}
    missing: List[dict] = []
    unverified: List[dict] = []
    for fact in facts:
        field = by_key.get(fact["fact_key"])
        data = _as_dict(fact.get("value"))
        value = data.get("value")
        if field is None or value not in (0, 1, 2):
            continue
        stats["checked"] += 1
        stats["present" if value == 1 else "missing" if value == 0 else "not_provided"] += 1
        stats["verified"] += 1 if data.get("verified") else 0
        stats["from_xml" if fact.get("source") == "eis_xml" else "from_llm"] += 1
        words = PRESENCE_WORDS if field["value_kind"] == "presence" else COMPLIANCE_WORDS
        item = {"field_key": field["field_key"], "criterion": short_label(field["label"]),
                "kind": field["value_kind"], "verdict": words[value], "comment": data.get("comment"),
                "quote": (fact.get("quote") or "")[:MAX_QUOTE] or None, "page": fact.get("page"),
                "source": fact.get("source")}
        if value == 0:
            missing.append(item)
        if not data.get("verified") and value != 2:
            unverified.append(item)
    return {"form": form_code, "stats": stats, "missing": missing, "unverified": unverified}


def compact_for_llm(digest: Dict[str, Any], limit: int = MAX_ITEMS_FOR_LLM) -> Dict[str, Any]:
    """Сжимает дайджест для промпта финальной проверки (контекст модели ограничен).

    Args:
        digest: Результат :func:`build_digest`.
        limit: Максимум критериев в списке.

    Returns:
        dict: ``{"checked", "present", "missing_count", "missing": [{"criterion", "verdict", "evidence"}]}``.
    """
    stats = digest["stats"]
    items = [{"criterion": m["criterion"][:160], "verdict": m["verdict"],
              "evidence": (m["quote"] or m["comment"] or "")[:120]} for m in digest["missing"][:limit]]
    return {"checked": stats["checked"], "present": stats["present"],
            "missing_count": stats["missing"], "missing": items}

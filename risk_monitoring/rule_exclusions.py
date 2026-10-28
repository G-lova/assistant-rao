"""Перепроверка сработавших показателей внешней системы по документам закупки.

Формулы показателей видят только поля данных ЕИС. Часть законных исключений видна лишь в тексте документов:
аванс выше 30 % с казначейским сопровождением, антидемпинговые меры, отсутствие гарантийных обязательств,
основание изменения контракта. Справочник таких исключений — ``config/rule_exclusions.json`` (согласован
с методистом).

Модель не отменяет правило (концепция, 2.8: «никакой AI не должен заменять правила»; 1.6: решение о нарушении — за
пользователем). Итог проверки — блок ``rule_check`` у риска:

* ``excluded`` — в документах есть исключение (цитата проверена по тексту): уровень риска ×
  ``RULE_EXCLUDED_LEVEL_FACTOR``, уверенность не выше ``RULE_EXCLUDED_CONFIDENCE``, в сводке — «возможно исключение»;
* ``confirmed`` — документы исключение не подтверждают: риск как есть, правило подтверждено документами;
* ``unclear`` — данных недостаточно или цитата не найдена в тексте: риск как есть.

Один запрос к модели на закупку — по всем её сработавшим показателям с исключениями.
"""
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence

from configs.logger import get_logger

logger = get_logger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "rule_exclusions.json")
STATUSES = ("excluded", "confirmed", "unclear")

SYSTEM = (
    "Ты — эксперт по контрактной системе (44-ФЗ). Для каждого сработавшего показателя риска проверь по фрагментам "
    "документов закупки, есть ли законное исключение из перечисленных. Отвечай только по тексту фрагментов. "
    "status: excluded — исключение прямо следует из текста (приведи точную цитату из фрагмента и file_id); "
    "confirmed — из текста видно, что исключения нет; unclear — в фрагментах об этом ничего нет. "
    "Не придумывай цитаты: цитата должна дословно совпадать с текстом фрагмента. reason — одно предложение по-русски.")

SCHEMA = {
    "type": "object",
    "properties": {"checks": {"type": "array", "items": {
        "type": "object",
        "properties": {"indicator": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)},
                       "exclusion_id": {"type": ["string", "null"]}, "reason": {"type": "string"},
                       "quote": {"type": ["string", "null"]}, "file_id": {"type": ["integer", "null"]}},
        "required": ["indicator", "status", "reason"]}}},
    "required": ["checks"],
}


@lru_cache(maxsize=1)
def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Справочник исключений (``version``, ``indicators``)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fired(v: Any) -> bool:
    """Показатель сработал: 1, «1», True."""
    if v is True:
        return True
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def fired_with_exclusions(values: Optional[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
                          suspect: Iterable[str] = ()) -> List[str]:
    """Сработавшие показатели, у которых в справочнике есть исключения (без показателей «на проверке»)."""
    cfg = cfg or load_config()
    bad = set(suspect or ())
    return [k for k, v in (values or {}).items() if k in cfg["indicators"] and k not in bad and _fired(v)]


def fragments(texts: Dict[int, str], names: Dict[int, str], keywords: Sequence[str], max_chars: int) -> List[Dict[str, Any]]:
    """Абзацы документов, где встречаются ключевые слова, — не больше ``max_chars`` символов всего.

    Args:
        texts: ``file_id → текст``.
        names: ``file_id → имя файла``.
        keywords: Основы слов (без учёта регистра).
        max_chars: Предел объёма.

    Returns:
        List[Dict[str, Any]]: ``file_id``, ``file_name``, ``text`` по документам.
    """
    kws = [k.lower() for k in keywords if k]
    out, used = [], 0
    for fid, text in texts.items():
        paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*\d+(?:\.\d+)*[.)]\s)", text or "") if p.strip()]
        hit = [p[:1500] for p in paras if any(k in p.lower() for k in kws)]
        if not hit:
            continue
        chunk = "\n…\n".join(hit)
        room = max_chars - used
        if room <= 200:
            break
        chunk = chunk[:room]
        out.append({"file_id": int(fid), "file_name": names.get(int(fid)) or str(fid), "text": chunk})
        used += len(chunk)
    return out


def build_prompt(keys: Sequence[str], frags: Sequence[Dict[str, Any]], cfg: Dict[str, Any],
                 titles: Dict[str, str]) -> str:
    """Сообщение модели: показатели, их исключения и фрагменты документов."""
    lines = ["Сработавшие показатели и возможные исключения:"]
    for k in keys:
        lines.append(f"- {k} ({titles.get(k, '')}):")
        for e in cfg["indicators"][k]["exclusions"]:
            lines.append(f"  * {e['id']} — {e['title']} ({e['norm']}). Вопрос: {e['question']}")
    lines.append("\nФрагменты документов закупки:")
    for f in frags:
        lines.append(f"\n[file_id={f['file_id']}] {f['file_name']}\n{f['text']}")
    lines.append("\nВерни checks — по одному элементу на каждый показатель.")
    return "\n".join(lines)


def verify(checks: Sequence[Dict[str, Any]], keys: Sequence[str], texts: Dict[int, str],
           cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Проверка ответа: исключение из справочника, цитата — в тексте указанного (или любого) документа.

    Исключение без подтверждённой цитаты становится ``unclear``.

    Returns:
        Dict[str, Dict[str, Any]]: ``показатель → rule_check``.
    """
    from knowledge_store.facts import find_quote

    out: Dict[str, Dict[str, Any]] = {}
    for c in checks or []:
        key = str(c.get("indicator") or "").strip()
        if key not in keys or key in out:
            continue
        status = c.get("status") if c.get("status") in STATUSES else "unclear"
        check = {"status": status, "reason": str(c.get("reason") or "")[:500], "version": cfg.get("version")}
        if status == "excluded":
            ids = {e["id"]: e for e in cfg["indicators"][key]["exclusions"]}
            exc = ids.get(c.get("exclusion_id")) or (next(iter(ids.values())) if len(ids) == 1 else None)
            quote, fid = c.get("quote"), c.get("file_id")
            pool = [(int(fid), texts[int(fid)])] if fid is not None and int(fid) in texts else list(texts.items())
            found = next(((f, q) for f, t in pool for q in [find_quote(quote, t)] if quote and q), None)
            if exc is None or found is None:
                check.update(status="unclear", reason=(check["reason"] + " (цитата не найдена в тексте документа)").strip())
            else:
                check.update(exclusion_id=exc["id"], exclusion=exc["title"], norm=exc["norm"],
                             evidence={"src": f"file:{found[0]}", "file_id": found[0], "fragment": found[1]})
        out[key] = check
    for k in keys:
        out.setdefault(k, {"status": "unclear", "reason": "модель не ответила по показателю", "version": cfg.get("version")})
    return out


def apply_checks(risks: List[Dict[str, Any]], checks: Dict[str, Dict[str, Any]], settings: Any) -> int:
    """Блок ``rule_check`` у рисков сработавших показателей; при исключении — понижение уровня и уверенности.

    Returns:
        int: Сколько рисков понижено (найдено исключение).
    """
    lowered = 0
    for r in risks:
        keys = [str(e.get("src"))[5:] for e in r.get("evidence") or [] if str(e.get("src", "")).startswith("rule:")]
        key = next((k for k in keys if k in checks), None)
        if key is None:
            continue
        check = checks[key]
        r["rule_check"] = {"indicator": key, **check}
        if check["status"] == "excluded":
            r["level"] = round((r.get("level") or 0) * settings.RULE_EXCLUDED_LEVEL_FACTOR, 3)
            r["confidence"] = min(r.get("confidence") or 1.0, settings.RULE_EXCLUDED_CONFIDENCE)
            if check.get("evidence") and check["evidence"] not in (r.get("evidence") or []):
                r.setdefault("evidence", []).append(check["evidence"])
            lowered += 1
    return lowered


async def check_rules(llm: Any, values: Optional[Dict[str, Any]], texts: Dict[int, str], names: Dict[int, str],
                      settings: Any, titles: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Перепроверка сработавших показателей закупки по её документам.

    Args:
        llm: ``LLMJson``.
        values: Значения показателей закупки.
        texts: ``file_id → текст`` действующих документов.
        names: ``file_id → имя файла``.
        settings: Настройки (``RULE_*``).
        titles: ``показатель → название`` (для запроса).

    Returns:
        Dict[str, Dict[str, Any]]: ``показатель → rule_check``; пусто — проверять нечего (нет сработавших показателей
        с исключениями, нет текстов или модели).
    """
    cfg = load_config()
    keys = fired_with_exclusions(values, cfg, getattr(settings, "RULE_SUSPECT_INDICATORS", ()))
    if not keys or not texts or llm is None:
        return {}
    kws = sorted({k for key in keys for k in cfg["indicators"][key]["keywords"]})
    frags = fragments(texts, names, kws, settings.RULE_EXCLUSION_MAX_CHARS)
    if not frags:
        return {k: {"status": "unclear", "reason": "в документах нет фрагментов по теме показателя",
                    "version": cfg.get("version")} for k in keys}
    data = await llm.ask(SYSTEM, build_prompt(keys, frags, cfg, titles or {}), schema=SCHEMA, max_tokens=2000)
    return verify(data.get("checks") or [], keys, texts, cfg)

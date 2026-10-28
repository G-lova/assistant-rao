"""Составные риски закупки (CR-*): сочетания факторов, которые вместе опаснее, чем по отдельности.

Шаблоны — ``risk_monitoring/config/composite_risks.json`` (их правят вместе с методистами, без изменения кода):

- ``all_of`` — список групп кодов; шаблон сработал, если в каждой группе есть хотя бы один фактор закупки;
- ``any_of`` + ``min_count`` — сработал, если из списка есть не меньше ``min_count`` разных факторов;
- ``level`` — уровень составного риска.

Фактор учитывается, если его вес (уровень × уверенность × затухание) не меньше ``min_component_weight`` и по
документам не найдено исключение (``rule_check.status = excluded``). Составной риск — отдельный список
``ai_analysis.composite_risks`` (во внешней таблице рисков документов его нет — PHP разбирает только ``risks``),
но он входит в балл закупки как ещё один фактор: уверенность — средняя по факторам, затухание — минимальное.
"""
import json
import os
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "composite_risks.json")


@lru_cache(maxsize=1)
def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Шаблоны составных рисков.

    Args:
        path: Путь к JSON.

    Returns:
        Dict[str, Any]: ``version``, ``min_component_weight``, ``patterns``.
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def titles() -> Dict[str, str]:
    """Названия составных рисков по коду (для сводок вместо кодов)."""
    return {p["code"]: p["title"] for p in load_config().get("patterns") or []}


def _weight(r: Dict[str, Any]) -> float:
    """Вес фактора: уровень × уверенность × затухание."""
    decay = r.get("decay_factor")
    return float(r.get("level") or 0) * float(r.get("confidence") or 0) * float(1.0 if decay is None else decay)


def detect(risks: Sequence[Dict[str, Any]], rejected: Iterable[str] = (), cfg: Optional[Dict[str, Any]] = None,
           prev: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Составные риски по рискам закупки.

    Args:
        risks: Риски закупки (после затухания и перепроверки исключений).
        rejected: Коды, отклонённые аналитиком (не учитываются).
        cfg: Шаблоны (по умолчанию :func:`load_config`).
        prev: Составные риски прошлого анализа — переносится ``first_detected_at``.

    Returns:
        List[Dict[str, Any]]: Риски во внешнем формате (``rule_id``, ``title``, ``description``, ``level``,
        ``confidence``, ``decay_factor``, ``kind = composite``, ``sources = [composite]``, ``components``,
        ``evidence``, ``version``), по убыванию веса.
    """
    cfg = cfg or load_config()
    floor = float(cfg.get("min_component_weight", 0.2))
    rejected = set(rejected)
    present: Dict[str, Dict[str, Any]] = {}
    for r in risks or []:
        code = r.get("rule_id")
        if not code or code in rejected or (r.get("rule_check") or {}).get("status") == "excluded":
            continue
        if _weight(r) >= floor and (code not in present or _weight(r) > _weight(present[code])):
            present[code] = r
    old = {r.get("rule_id"): r for r in prev or []}
    out: List[Dict[str, Any]] = []
    for p in cfg.get("patterns") or []:
        if p.get("all_of"):
            picks = []
            for group in p["all_of"]:
                hit = [present[c] for c in group if c in present]
                if not hit:
                    picks = []
                    break
                picks.append(max(hit, key=_weight))
        else:
            picks = [present[c] for c in p.get("any_of") or [] if c in present]
            if len(picks) < int(p.get("min_count") or 2):
                picks = []
        if not picks:
            continue
        comps = list({r["rule_id"]: r for r in picks}.values())
        conf = round(sum(float(r.get("confidence") or 0) for r in comps) / len(comps), 3)
        decay = min(float(1.0 if r.get("decay_factor") is None else r["decay_factor"]) for r in comps)
        item = {"rule_id": p["code"], "title": p["title"], "description": p.get("description"),
                "level": float(p.get("level", 0.5)), "confidence": conf, "decay_factor": round(decay, 3),
                "kind": "composite", "sources": ["composite"],
                "components": [{"rule_id": r["rule_id"], "title": r.get("title")} for r in comps],
                "evidence": [{"src": f"risk:{r['rule_id']}", "fragment": r.get("title")} for r in comps],
                "version": cfg.get("version")}
        if (old.get(p["code"]) or {}).get("first_detected_at"):
            item["first_detected_at"] = old[p["code"]]["first_detected_at"]
        out.append(item)
    out.sort(key=_weight, reverse=True)
    return out

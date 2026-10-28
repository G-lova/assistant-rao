"""Динамика риска закупки (концепция «Система риск-мониторинга», гл. 10–11).

Чистые функции; данные готовит :class:`risk_monitoring.contract_analyzer.ContractAnalyzer`.

* **События риска (11.7)** — сравнение рисков закупки с прошлым анализом: RSK-001 новый, RSK-002 повышение,
  RSK-003 снижение, RSK-004 закрытие, RSK-005 подтверждение аналитиком, RSK-006 отклонение аналитиком.
* **Затухание во времени (10.10)** — у риска дата первого выявления; вес риска в балле умножается на
  ``0,5 ^ (возраст / период полураспада)``. Период — по коду (``RISK_HALF_LIFE_DAYS``); ограничение конкуренции,
  нарушения исполнения, документы и реестры не затухают (документ сменяется новой редакцией, реестр — записью).
* **Индекс доверия (10.15)** — насколько можно доверять баллу: полнота данных, документы и качество распознавания,
  сила доказательств главных факторов (правило, подтверждение моделью, только модель, исключение по документам).
* **Приоритет проверки (10.14)** — не равен риску: учитывает рост балла, сумму закупки, новизну событий и доверие.
"""
import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

RSK_TITLES = {
    "RSK-001": "Новый риск", "RSK-002": "Повышение риска", "RSK-003": "Снижение риска", "RSK-004": "Закрытие риска",
    "RSK-005": "Подтверждение аналитиком", "RSK-006": "Отклонение аналитиком",
}
AI_ONLY = {"ai_file", "ai_event", "version_diff", "ai"}


def _now(now: Optional[datetime] = None) -> datetime:
    """Текущий момент в UTC."""
    return now or datetime.now(timezone.utc)


def _parse(ts: Any) -> Optional[datetime]:
    """Момент из ISO-строки (с поясом или без — тогда UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _w(r: Dict[str, Any]) -> float:
    """Сила риска: уровень × уверенность."""
    return float(r.get("level") or 0) * float(r.get("confidence") if r.get("confidence") is not None else 1.0)


# ------------------------------------------------------------------ события риска (11.7)
def risk_changes(prev: Sequence[Dict[str, Any]], new: Sequence[Dict[str, Any]], prev_confirmed: Iterable[str] = (),
                 confirmed: Iterable[str] = (), prev_rejected: Iterable[str] = (), rejected: Iterable[str] = (),
                 threshold: float = 0.05) -> List[Dict[str, Any]]:
    """События риска закупки относительно прошлого анализа.

    Args:
        prev: Риски прошлого анализа (``ai_analysis.risks``); пусто — первая оценка (событий нет: всё было бы «новым»).
        new: Риски текущего анализа.
        prev_confirmed, confirmed: Коды, подтверждённые аналитиком, до и после.
        prev_rejected, rejected: Коды, отклонённые аналитиком, до и после.
        threshold: Изменение силы риска (уровень × уверенность), считающееся повышением или снижением.

    Returns:
        List[Dict[str, Any]]: ``code`` (RSK-*), ``title``, ``rule_id``, ``risk_title``, ``old``, ``new`` (сила риска).
    """
    if not prev:
        return []
    old = {r["rule_id"]: r for r in prev if r.get("rule_id")}
    cur = {r["rule_id"]: r for r in new if r.get("rule_id")}
    out: List[Dict[str, Any]] = []

    def add(code: str, rule_id: str, r: Optional[Dict[str, Any]], o: Optional[float], n: Optional[float]) -> None:
        """Событие риска."""
        out.append({"code": code, "title": RSK_TITLES[code], "rule_id": rule_id,
                    "risk_title": (r or {}).get("title") or (old.get(rule_id) or {}).get("title"),
                    "old": round(o, 3) if o is not None else None, "new": round(n, 3) if n is not None else None})

    for code, r in cur.items():
        if code not in old:
            add("RSK-001", code, r, None, _w(r))
        else:
            d = _w(r) - _w(old[code])
            if d >= threshold:
                add("RSK-002", code, r, _w(old[code]), _w(r))
            elif d <= -threshold:
                add("RSK-003", code, r, _w(old[code]), _w(r))
    for code, r in old.items():
        if code not in cur:
            add("RSK-004", code, r, _w(r), None)
    for code in sorted(set(confirmed) - set(prev_confirmed)):
        add("RSK-005", code, cur.get(code), None, _w(cur[code]) if code in cur else None)
    for code in sorted(set(rejected) - set(prev_rejected)):
        add("RSK-006", code, cur.get(code), _w(old[code]) if code in old else None, None)
    return out


# ------------------------------------------------------------------ затухание (10.10)
def half_life(code: str, table: Dict[str, Optional[int]]) -> Optional[int]:
    """Период полураспада риска, дней: по коду, затем по категории (префикс до «-»); ``None`` — не затухает."""
    if code in table:
        return table[code]
    return table.get(code.split("-")[0])


def apply_decay(risks: List[Dict[str, Any]], prev: Sequence[Dict[str, Any]], table: Dict[str, Optional[int]],
                now: Optional[datetime] = None) -> int:
    """Дата первого выявления и коэффициент затухания рисков.

    ``first_detected_at`` переносится из прошлого анализа (риск того же кода), иначе — сейчас. Подтверждённый
    аналитиком и вновь усилившийся риск не затухает: усиление — новое событие, отсчёт заново.

    Args:
        risks: Риски закупки (меняются на месте: ``first_detected_at``, ``decay_factor``).
        prev: Риски прошлого анализа.
        table: ``RISK_HALF_LIFE_DAYS``.
        now: Момент расчёта.

    Returns:
        int: Сколько рисков ослаблено (коэффициент < 1).
    """
    t = _now(now)
    old = {r["rule_id"]: r for r in prev or [] if r.get("rule_id")}
    lowered = 0
    for r in risks:
        p = old.get(r.get("rule_id"))
        first = _parse((p or {}).get("first_detected_at")) if p else None
        if p is not None and _w(r) - _w(p) >= 0.05:
            first = None          # риск усилился — новое событие, затухание с начала
        first = first or t
        r["first_detected_at"] = first.isoformat(timespec="seconds")
        hl = half_life(r.get("rule_id") or "", table)
        factor = 1.0
        if hl and not r.get("analyst_confirmed"):
            age = max(0.0, (t - first).total_seconds() / 86400)
            factor = round(0.5 ** (age / hl), 3)
        r["decay_factor"] = factor
        if factor < 1:
            lowered += 1
    return lowered


# ------------------------------------------------------------------ индекс доверия (10.15)
def _strength(r: Dict[str, Any]) -> float:
    """Сила доказательства фактора: правило / реестр / подтверждено аналитиком — 1; правило, подтверждённое
    моделью, — 1; только модель с проверенной цитатой — 0,6; только модель без цитаты — 0,4; исключение по
    документам — 0,3."""
    if (r.get("rule_check") or {}).get("status") == "excluded":
        return 0.3
    if r.get("analyst_confirmed") or r.get("confirmed"):
        return 1.0
    sources = set(r.get("sources") or [])
    if sources and not sources <= AI_ONLY:
        return 1.0
    quoted = any(isinstance(e, dict) and (e.get("quote_verified") or e.get("fragment")) for e in r.get("evidence") or [])
    return 0.6 if quoted else 0.4


def confidence_index(passport: Dict[str, Any], package: Dict[str, Any], risks: Sequence[Dict[str, Any]],
                     files: Sequence[Dict[str, Any]], rules_present: bool) -> Dict[str, Any]:
    """Индекс доверия к оценке закупки, 0–100.

    Составляющие (0–1):

    * ``data`` — полнота данных ЕИС: НМЦК, способ, предмет, заказчик, цена контракта (если контракт заключён),
      расчёт правил внешней системы;
    * ``documents`` — доля проанализированных документов × доля документов без сбоя распознавания;
    * ``evidence`` — средняя сила доказательств пяти главных рисков (:func:`_strength`); без рисков — 1.

    ``score = 100 × (0,3 × data + 0,3 × documents + 0,4 × evidence)``.

    Returns:
        Dict[str, Any]: ``score``, ``components``, ``notes`` (почему доверие снижено).
    """
    p = passport or {}
    checks = [p.get("nmck") is not None, bool(p.get("method") or p.get("method_code")), bool(p.get("subject")),
              bool((p.get("customer") or {}).get("inn") or (p.get("customer") or {}).get("name")), rules_present]
    if p.get("stage") not in (None, "notice"):
        checks.append(p.get("contract_price") is not None)
    data = sum(1 for c in checks if c) / len(checks)
    total = (package or {}).get("files_total") or 0
    analyzed = (package or {}).get("files_analyzed") or 0
    active = [f for f in files or [] if not f.get("superseded_by")]
    bad = sum(1 for f in active if f.get("rm_result") in ("ocr_bad", "no_text") or f.get("text_quality") in ("bad", "suspect"))
    documents = (analyzed / total) * (1 - bad / len(active) if active else 1.0) if total else 0.5
    top = sorted(risks or [], key=_w, reverse=True)[:5]
    evidence = sum(_strength(r) for r in top) / len(top) if top else 1.0
    notes = []
    if data < 1:
        notes.append("в данных ЕИС не хватает сведений")
    if total and analyzed < total:
        notes.append(f"проанализировано документов {analyzed} из {total}")
    if bad:
        notes.append(f"документов с плохим распознаванием: {bad}")
    if top and evidence < 0.7:
        notes.append("главные риски найдены только моделью или по документам возможны исключения")
    score = round(100 * (0.3 * data + 0.3 * documents + 0.4 * evidence), 1)
    return {"score": score, "components": {"data": round(data, 2), "documents": round(documents, 2),
                                          "evidence": round(evidence, 2)}, "notes": notes}


# ------------------------------------------------------------------ приоритет проверки (10.14)
def priority(score: Optional[float], delta: Optional[float], nmck: Optional[float], changes: Sequence[Dict[str, Any]],
             confidence: Optional[float], weights: Dict[str, float]) -> Dict[str, Any]:
    """Приоритет проверки закупки, 0–100.

    Составляющие (0–100): ``risk`` — балл; ``growth`` — рост балла (×4, 25 баллов роста → 100);
    ``money`` — НМЦК по логарифмической шкале (100 тыс. → 0, 1 млн → 20, 1 млрд → 80); ``novelty`` — новые или
    усилившиеся риски в этом анализе (100), иные изменения рисков (50). Взвешенная сумма умножается на
    ``0,5 + 0,5 × доверие``: при низком доверии объект поднимается ниже, а сводка советует дополнительную проверку.

    Returns:
        Dict[str, Any]: ``score``, ``components``, ``reasons`` (словами, для сводки).
    """
    risk = float(score or 0)
    growth = min(100.0, max(0.0, float(delta or 0)) * 4)
    money = min(100.0, max(0.0, 20 * math.log10(float(nmck) / 1e5))) if nmck and float(nmck) > 0 else 0.0
    kinds = {c.get("code") for c in changes or []}
    novelty = 100.0 if kinds & {"RSK-001", "RSK-002"} else 50.0 if kinds else 0.0
    base = (weights.get("risk", 0.4) * risk + weights.get("growth", 0.25) * growth
            + weights.get("money", 0.15) * money + weights.get("novelty", 0.2) * novelty)
    conf = 1.0 if confidence is None else max(0.0, min(1.0, float(confidence) / 100))
    value = round(base * (0.5 + 0.5 * conf), 1)
    reasons = []
    if growth >= 40:
        reasons.append(f"балл вырос на {float(delta):g}")
    if "RSK-001" in kinds:
        reasons.append("новые риски")
    elif "RSK-002" in kinds:
        reasons.append("риски усилились")
    if money >= 40:
        reasons.append("крупная сумма")
    if risk >= 70:
        reasons.append("высокий балл")
    if conf < 0.6:
        reasons.append("низкое доверие к оценке — нужна дополнительная проверка")
    return {"score": value, "components": {"risk": round(risk, 1), "growth": round(growth, 1), "money": round(money, 1),
                                          "novelty": novelty, "confidence": round(conf * 100, 1)}, "reasons": reasons}


# ------------------------------------------------------------------ еженедельный пересчёт затухания (10.10)
def recalc_decay(ai: Dict[str, Any], cfg: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Пересчёт затухания и балла сохранённого ``ai_analysis`` закупки без нового анализа документов.

    Ежедневный прогон пропускает закупки с неизменными входными данными, поэтому старые риски сами по себе
    не ослабевают — это делает еженедельная задача. ``first_detected_at`` берётся из самих рисков; уровни и
    уверенности не меняются, меняется только ``decay_factor`` и всё, что от него зависит (балл, зона,
    интегральный риск, приоритет). События риска (``risk_changes``) не пересчитываются: новых событий не было.

    Args:
        ai: Сохранённый ``ai_analysis`` закупки.
        cfg: Настройки (``RISK_HALF_LIFE_DAYS``, ``RISK_DECAY_MIN_CHANGE``, ``PRIORITY_WEIGHTS`` и параметры балла).
        now: Момент расчёта.

    Returns:
        Optional[Dict[str, Any]]: Обновлённый ``ai_analysis`` (копия) — если балл изменился не меньше чем на
        ``RISK_DECAY_MIN_CHANGE``; иначе ``None``.
    """
    import copy

    from risk_monitoring import contract_package as cp
    from risk_monitoring import passport

    risks = (ai or {}).get("risks")
    prof = (ai or {}).get("profile") or {}
    if not risks or prof.get("score") is None:
        return None
    new = copy.deepcopy(ai)
    # прошлое — сами же риски: даты первого выявления сохраняются, «усиления» нет
    apply_decay(new["risks"], ai["risks"], cfg.RISK_HALF_LIFE_DAYS, now)
    new["risks"].sort(key=lambda r: (r.get("level") or 0) * (r.get("confidence") or 0), reverse=True)
    new["profile"] = cp.profile(new["risks"], prof.get("previous_score"), new.get("confirmed_codes") or (),
                                cfg.CONFIRMED_WEIGHT, tuple(cfg.SCORE_ZONES), cfg.SCORE_DECAY, cfg.AI_UNCONFIRMED_WEIGHT)
    if abs(float(new["profile"].get("score") or 0) - float(prof.get("score") or 0)) < cfg.RISK_DECAY_MIN_CHANGE:
        return None
    new["total_risk"] = passport.total_risk(new["risks"])
    conf = (new.get("confidence") or {}).get("score")
    new["priority"] = priority(new["profile"].get("score"), new["profile"].get("delta"),
                               (new.get("passport") or {}).get("nmck"), (), conf, cfg.PRIORITY_WEIGHTS)
    pipe = dict(new.get("pipeline") or {})
    pipe["decay_recalc_at"] = _now(now).isoformat(timespec="seconds")
    new["pipeline"] = pipe
    return new

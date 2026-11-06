"""Решения аналитиков по рискам закупки и пересчёт балла после решения без нового анализа (05.11.2026).

Дашборды пишут решение по коду риска во все его строки ``risk_monitoring_document_risks``: строку закупки
(``scope_type = contract``), строки файлов (``file``) и событий (``xml``). Раньше сервис читал решения только у строк
файлов, поэтому отклонение риска правила или события не меняло балл, а закупки без новых документов не
пересчитывались совсем.

Правила (одни для полного анализа и для пересчёта):

* **код отклонён или устранён** (статус строки закупки 2 / 4) — риск кода и составной риск с этим кодом в балл не
  идут. Исключение — новые доказательства после решения: строка файла или события того же кода ещё на
  рассмотрении (0) — риск остаётся (дашборд возвращает код в очередь с пометкой «новые доказательства»);
* **«Не относится»** — отклонена строка одного документа. Если отклонены все строки документов, на которые
  ссылается риск, и других источников (правило, данные ЕИС) у него нет — риск в балл не идёт;
* **подтверждён** (1 у строки закупки или у строки документа) — риск весит ``CONFIRMED_WEIGHT``;
* снятые риски хранятся в ``ai_analysis.rejected_risks``: если решение отменят, пересчёт вернёт риск в балл.

Пересчёт (:func:`recalc`) — по сохранённому ``ai_analysis``: состав рисков, составные риски, балл, зона, вклад в
балл, интегральный риск и приоритет. Задача ``rm_decisions_task`` раз в несколько минут находит закупки с
изменёнными строками рисков и пересчитывает те, у которых изменился отпечаток решений
(``pipeline.decisions_hash``).
"""
import copy
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

IGNORED = {2, 4}         # отклонён, устранён
CONFIRMED = 1
REJECTED = 2
PENDING = 0              # на рассмотрении

Row = Tuple[str, int, str]   # (scope_type, scope_id, rule_id)


def _int(v: Any) -> Optional[int]:
    """Целое из значения БД; ``None`` — пусто или не число."""
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


def group_rows(rows: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Строки ``risk_decisions.sql`` по закупкам.

    Args:
        rows: ``contract_id``, ``scope_type``, ``scope_id``, ``rule_id``, ``status``.

    Returns:
        Dict[int, Dict[str, Any]]: ``contract_id → {"codes": {rule_id: статус строки закупки},
        "rows": {(scope_type, scope_id, rule_id): статус}}``.
    """
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows or []:
        cid, sid, st = _int(r.get("contract_id")), _int(r.get("scope_id")), _int(r.get("status"))
        scope, code = str(r.get("scope_type") or "file"), r.get("rule_id")
        if cid is None or sid is None or st is None or not code:
            continue
        entry = out.setdefault(cid, {"codes": {}, "rows": {}})
        entry["rows"][(scope, sid, code)] = st
        if scope == "contract":
            entry["codes"][code] = st
    return out


def decisions_hash(entry: Optional[Dict[str, Any]]) -> str:
    """Отпечаток решений по закупке: только строки с решением (статус ≠ 0).

    Строки «на рассмотрении» не входят: дашборды создают их при каждом приёме анализа (в том числе строки составных
    рисков), и без этого после каждого анализа шёл лишний пересчёт (06.11.2026).
    """
    rows = sorted(f"{s}:{i}:{c}={st}" for (s, i, c), st in ((entry or {}).get("rows") or {}).items()
                  if st != PENDING)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _refs(risk: Dict[str, Any]) -> Tuple[List[Tuple[str, int]], bool]:
    """Строки документов риска (``file:<id>`` / ``xml:<id>``) и есть ли у него другие источники (правило, ЕИС…)."""
    docs: List[Tuple[str, int]] = []
    other = not (risk.get("evidence") or [])
    for e in risk.get("evidence") or []:
        src = str((e or {}).get("src") or "") if isinstance(e, dict) else ""
        scope, _, rid = src.partition(":")
        if scope in ("file", "xml") and rid.isdigit():
            if (scope, int(rid)) not in docs:
                docs.append((scope, int(rid)))
        else:
            other = True
    return docs, other


def excluded(risk: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> bool:
    """Снят ли риск решениями аналитика (правила — в описании модуля).

    Args:
        risk: Риск закупки (или составной риск).
        entry: Решения по закупке (:func:`group_rows`); ``None`` — решений нет.

    Returns:
        bool: ``True`` — риск в балл не идёт.
    """
    if not entry:
        return False
    code = risk.get("rule_id")
    rows = entry.get("rows") or {}
    docs, other = _refs(risk)
    if entry.get("codes", {}).get(code) in IGNORED:
        if risk.get("kind") == "composite":
            return True
        return not any(rows.get((s, i, code)) == PENDING for s, i in docs)
    return bool(docs) and not other and all(rows.get((s, i, code)) in IGNORED for s, i in docs)


def confirmed_codes(risks: Sequence[Dict[str, Any]], entry: Optional[Dict[str, Any]]) -> set:
    """Коды, подтверждённые аналитиком: строка закупки или строка документа риска со статусом 1."""
    if not entry:
        return set()
    rows = entry.get("rows") or {}
    out = {c for c, st in (entry.get("codes") or {}).items() if st == CONFIRMED}
    for r in risks:
        code = r.get("rule_id")
        if any(rows.get((s, i, code)) == CONFIRMED for s, i in _refs(r)[0]):
            out.add(code)
    return out


def split(risks: Sequence[Dict[str, Any]], kept_out: Sequence[Dict[str, Any]],
          entry: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Риски закупки → (учитываемые, снятые решениями). Снятые раньше риски возвращаются, если решение отменили.

    Args:
        risks: Текущие риски ``ai_analysis.risks``.
        kept_out: Снятые раньше (``ai_analysis.rejected_risks``).
        entry: Решения по закупке.

    Returns:
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: Учитываемые и снятые риски.
    """
    seen = {r.get("rule_id") for r in risks}
    pool = list(risks) + [r for r in kept_out or [] if r.get("rule_id") not in seen]
    keep, out = [], []
    for r in pool:
        (out if excluded(r, entry) else keep).append(r)
    return keep, out


def apply(built: Dict[str, Any], entry: Optional[Dict[str, Any]], prev_out: Sequence[Dict[str, Any]] = ()) -> None:
    """Решения аналитика в результате анализа закупки (на месте): состав рисков, ``rejected_risks``,
    ``confirmed_codes``, ``rejected_codes`` (с кодами составных рисков), ``pipeline_decisions_hash``.

    Args:
        built: Результат ``build_contract_analysis`` или сохранённый ``ai_analysis``.
        entry: Решения по закупке (:func:`group_rows`).
        prev_out: Снятые раньше риски (``ai_analysis.rejected_risks`` прошлого результата).
    """
    if not entry:
        # решений нет или запрос не удался: снятые раньше риски не возвращаются, состав рисков не меняется
        built["rejected_risks"] = list(prev_out or [])
        built["decisions_hash"] = decisions_hash(entry)
        return
    keep, out = split(built.get("risks") or [], prev_out, entry)
    built["risks"] = keep
    built["rejected_risks"] = out
    codes = (entry or {}).get("codes") or {}
    confirmed = set(built.get("confirmed_codes") or []) | confirmed_codes(keep, entry)
    confirmed -= {c for c, st in codes.items() if st in IGNORED}
    keep_codes = {r.get("rule_id") for r in keep}
    rejected = set(built.get("rejected_codes") or []) | {c for c, st in codes.items() if st == REJECTED}
    built["confirmed_codes"] = sorted(c for c in confirmed if c in keep_codes or c.startswith("CR-"))
    built["rejected_codes"] = sorted(rejected - keep_codes)
    for r in keep:
        if r.get("rule_id") in confirmed:
            r["analyst_confirmed"] = True
        else:
            r.pop("analyst_confirmed", None)
    built["decisions_hash"] = decisions_hash(entry)


def recalc(ai: Dict[str, Any], entry: Optional[Dict[str, Any]], cfg: Any,
           now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Пересчёт сохранённого ``ai_analysis`` закупки после решений аналитика, без модели.

    Args:
        ai: Сохранённый ``ai_analysis`` закупки.
        entry: Решения по закупке (:func:`group_rows`).
        cfg: Настройки (параметры балла, ``PRIORITY_WEIGHTS``, ``COMPOSITE_RISKS_ENABLED``).
        now: Момент пересчёта (для ``pipeline.decisions_recalc_at``).

    Returns:
        Optional[Dict[str, Any]]: Новый ``ai_analysis`` (копия); ``None`` — решения не менялись с прошлого расчёта,
        строк решений нет (запрос не удался — подтверждения не сбрасываются) или у закупки нет балла.
    """
    from risk_monitoring import contract_package as cp
    from risk_monitoring import passport
    from risk_monitoring import risk_dynamics as rd

    if not ai or not entry or (ai.get("profile") or {}).get("score") is None:
        return None
    h = decisions_hash(entry)
    if (ai.get("pipeline") or {}).get("decisions_hash") == h:
        return None
    new = copy.deepcopy(ai)
    new["confirmed_codes"] = []          # пересобираются из строк решений
    apply(new, entry, ai.get("rejected_risks") or ())
    new.pop("decisions_hash", None)
    new["risks"].sort(key=lambda r: (r.get("level") or 0) * (r.get("confidence") or 0), reverse=True)
    if getattr(cfg, "COMPOSITE_RISKS_ENABLED", False):
        from risk_monitoring import composite_risks as cr

        new["composite_risks"] = [c for c in cr.detect(new["risks"], new.get("rejected_codes") or (),
                                                       prev=ai.get("composite_risks") or ())
                                  if not excluded(c, entry)]
    scored = list(new["risks"]) + list(new.get("composite_risks") or [])
    prof = ai.get("profile") or {}
    new["profile"] = cp.profile(scored, prof.get("previous_score"), new.get("confirmed_codes") or (),
                                cfg.CONFIRMED_WEIGHT, tuple(cfg.SCORE_ZONES), cfg.SCORE_DECAY, cfg.AI_UNCONFIRMED_WEIGHT)
    new["total_risk"] = passport.total_risk(scored)
    conf = (new.get("confidence") or {}).get("score")
    new["priority"] = rd.priority(new["profile"].get("score"), new["profile"].get("delta"),
                                  (new.get("passport") or {}).get("nmck"), (), conf, cfg.PRIORITY_WEIGHTS)
    if new.get("not_in_score"):
        new["not_in_score"] = cp.not_in_score(new["not_in_score"], new["risks"])
    pipe = dict(new.get("pipeline") or {})
    pipe["decisions_hash"] = h
    # тот же формат, что analyzed_at (локальное время сервиса без смещения) — 06.11.2026
    pipe["decisions_recalc_at"] = (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    new["pipeline"] = pipe
    return new

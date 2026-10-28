"""Недельная и месячная сводки риск-мониторинга — из сохранённых ежедневных сводок (``ai_rm_results``,
``kind = daily``), без новых запросов к внешней БД.

Период:

- неделя — ISO-неделя (понедельник–воскресенье), обозначение ``2026-W43``, id ``202643``, ``kind = weekly``;
- месяц — календарный, обозначение ``2026-10``, id ``202610``, ``kind = monthly``.

Сводка собирается кодом (:func:`build_period_summary`), текст — моделью по фактам (:func:`period_facts`) или
шаблоном. Дни без готовой ежедневной сводки перечисляются (``days.missing``, ``days.partial``), сводка периода с
такими днями — ``partial`` и пересобирается задачей ``rm_period_task``, когда дни догонятся.
"""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from configs import settings
from configs.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = "rm-period-1"
PROMPT_VERSION = "period-resume-1"
KINDS = {"week": "weekly", "month": "monthly"}
NAMES = {"week": "неделю", "month": "месяц"}
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь")

RESUME_SYSTEM = (
    "Ты — аналитик риск-мониторинга госзакупок организаций, подведомственных Минобрнауки России. Тебе дан список "
    "фактов сводки за {period}. Напиши сводку для руководителя: 5–8 предложений, по-русски, без вступлений — что "
    "изменилось за период и по сравнению с прошлым периодом, какие закупки и заказчики требуют внимания в первую "
    "очередь, что проверить. Используй только эти факты: не добавляй цифр и выводов, которых в них нет. Если в "
    "периоде есть дни без сводки — скажи, что сводка неполная. Не используй коды рисков и правил (вида DOC-002, "
    "CR-001, pz_p1) — называй риски словами. Ответ — обычный текст, без JSON и разметки.")


# ------------------------------------------------------------------ периоды
def period_bounds(kind: str, period: str) -> Tuple[date, date]:
    """Первый и последний день периода.

    Args:
        kind: ``week`` / ``month``.
        period: ``2026-W43`` / ``2026-10``.

    Returns:
        Tuple[date, date]: Границы включительно.

    Raises:
        ValueError: Неверный вид или обозначение периода.
    """
    if kind == "week":
        y, w = period.upper().split("-W")
        start = date.fromisocalendar(int(y), int(w), 1)
        return start, start + timedelta(days=6)
    if kind == "month":
        y, m = (int(x) for x in period.split("-"))
        start = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        return start, nxt - timedelta(days=1)
    raise ValueError("вид периода — week или month")


def period_of(kind: str, d: date) -> str:
    """Обозначение периода, в который входит дата."""
    if kind == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{d.year}-{d.month:02d}"


def previous_period(kind: str, period: str) -> str:
    """Предыдущий период (для сравнения)."""
    start, _ = period_bounds(kind, period)
    return period_of(kind, start - timedelta(days=1))


def last_closed(kind: str, today: date) -> str:
    """Последний завершившийся период: прошлая неделя / прошлый месяц."""
    if kind == "week":
        return period_of(kind, today - timedelta(days=today.isoweekday()))
    return period_of(kind, today.replace(day=1) - timedelta(days=1))


def period_key(kind: str, period: str) -> int:
    """id сводки периода: неделя ``2026-W43`` → ``202643``, месяц ``2026-10`` → ``202610``."""
    start, _ = period_bounds(kind, period)
    if kind == "week":
        y, w, _ = start.isocalendar()
        return y * 100 + w
    return start.year * 100 + start.month


def period_title(kind: str, period: str) -> str:
    """Период словами: «неделю 20.10–26.10.2026» / «октябрь 2026»."""
    start, end = period_bounds(kind, period)
    if kind == "week":
        return f"неделю {start:%d.%m}–{end:%d.%m.%Y}"
    return f"{MONTHS[start.month - 1]} {start.year}"


def days(kind: str, period: str) -> List[str]:
    """Даты периода ``ГГГГ-ММ-ДД``."""
    start, end = period_bounds(kind, period)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


# ------------------------------------------------------------------ сборка
def _add(acc: Dict[str, int], part: Optional[Dict[str, Any]]) -> None:
    """Сумма счётчиков по ключам."""
    for k, v in (part or {}).items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + int(v)


def _latest(items: Iterable[Tuple[str, Dict[str, Any]]], key: str = "card_id") -> Dict[Any, Dict[str, Any]]:
    """Последнее по дате упоминание объекта (``(дата, элемент)`` → ``id → элемент`` с ``date``)."""
    out: Dict[Any, Dict[str, Any]] = {}
    for d, it in sorted(items, key=lambda x: x[0]):
        k = it.get(key)
        if k is not None:
            out[k] = {**it, "date": d}
    return out


def build_period_summary(kind: str, period: str, dailies: Dict[str, Dict[str, Any]],
                         previous: Optional[Dict[str, Any]] = None, today: Optional[date] = None,
                         cfg: Any = settings) -> Dict[str, Any]:
    """Сводка периода по ежедневным сводкам.

    Args:
        kind: ``week`` / ``month``.
        period: Обозначение периода.
        dailies: ``дата → ai_analysis`` ежедневной сводки (только найденные).
        previous: Сводка прошлого периода (для сравнения) или ``None``.
        today: Сегодня (будущие дни периода не считаются пропущенными).
        cfg: Настройки (``DAILY_TOP``).

    Returns:
        Dict[str, Any]: ``period``, ``days``, ``events``, ``contracts``, ``risk_changes``, ``composite_risks``,
        ``priorities``, ``customers``, ``attention``, ``execution_open_past_plan``, ``comparison``, ``input_hash``.
    """
    today = today or datetime.now(timezone.utc).date()
    top = cfg.DAILY_TOP
    all_days = days(kind, period)
    done, partial, missing, future = [], [], [], []
    for d in all_days:
        s = dailies.get(d)
        status = ((s or {}).get("pipeline") or {}).get("status")
        if d >= today.isoformat() and not s:
            future.append(d)
        elif status == "completed":
            done.append(d)
        elif s:
            partial.append(d)
        else:
            missing.append(d)
    used = {d: s for d, s in dailies.items() if d in all_days}
    ev_total, ev_types, rsk, comp, attn = 0, {}, {}, {}, {}
    important, became_red, jumps, new_r, closed_r, prios, custs = [], [], [], [], [], [], []
    changed = 0
    for d, s in used.items():
        ev = s.get("events") or {}
        ev_total += int(ev.get("total") or 0)
        _add(ev_types, ev.get("by_type"))
        important += [(d, e) for e in ev.get("important") or []]
        c = s.get("contracts") or {}
        changed += int(c.get("changed") or 0)
        became_red += [(d, i) for i in c.get("became_red") or []]
        jumps += [(d, i) for i in c.get("score_jump") or []]
        rc = s.get("risk_changes") or {}
        _add(rsk, rc.get("by_type"))
        new_r += [(d, i) for i in rc.get("new") or []]
        closed_r += [(d, i) for i in rc.get("closed") or []]
        _add(comp, s.get("composite_risks"))
        prios += [(d, i) for i in s.get("priorities") or []]
        custs += [(d, i) for i in s.get("customers") or []]
        for a in s.get("attention") or []:
            attn[a.get("kind")] = attn.get(a.get("kind"), 0) + 1
    last_day = max(used) if used else None
    red = _latest(became_red)
    jump = sorted(_latest(jumps).values(), key=lambda i: -(i.get("delta") or 0))
    prio = sorted(_latest(prios).values(), key=lambda i: -(i.get("priority") or 0))
    cust = sorted(_latest(custs, "organisation_id").values(), key=lambda c: -(c.get("score") or 0))
    imp = sorted(important, key=lambda x: (-len(x[1].get("risks") or []), x[0]))
    new_list = [{**i, "date": d} for d, i in sorted(new_r, key=lambda x: x[0])]
    closed_list = [{**i, "date": d} for d, i in sorted(closed_r, key=lambda x: x[0])]
    out = {
        "period": {"kind": kind, "period": period, "title": period_title(kind, period),
                   "from": all_days[0], "to": all_days[-1]},
        "days": {"total": len(all_days), "completed": done, "partial": partial, "missing": missing, "future": future},
        "events": {"total": ev_total, "by_type": dict(sorted(ev_types.items(), key=lambda kv: -kv[1])),
                   "important": [{**e, "date": d} for d, e in imp[:top]]},
        "contracts": {"recalculated": changed, "became_red": len(red),
                      "became_red_list": sorted(red.values(), key=lambda i: -(i.get("score") or 0))[:top],
                      "score_jump": jump[:top]},
        "risk_changes": {"by_type": rsk, "new": new_list[-top:], "closed": closed_list[-top:],
                         "new_total": len(new_list), "closed_total": len(closed_list)},
        "composite_risks": dict(sorted(comp.items(), key=lambda kv: -kv[1])),
        "priorities": prio[:top],
        "customers": cust[:top],
        "attention": dict(sorted(attn.items(), key=lambda kv: -kv[1])),
        "execution_open_past_plan": (used[last_day].get("execution_open_past_plan") if last_day else None),
    }
    if previous:
        pe, pc, pr = previous.get("events") or {}, previous.get("contracts") or {}, previous.get("risk_changes") or {}
        out["comparison"] = {"period": (previous.get("period") or {}).get("period"),
                             "events": ev_total - int(pe.get("total") or 0),
                             "became_red": len(red) - int(pc.get("became_red") or 0),
                             "new_risks": len(new_list) - int(pr.get("new_total") or 0),
                             "closed_risks": len(closed_list) - int(pr.get("closed_total") or 0)}
    out["input_hash"] = hashlib.sha256(json.dumps(
        [[d, ((s.get("pipeline") or {}).get("finished_at"))] for d, s in sorted(used.items())]
        + [(previous or {}).get("input_hash")], default=str).encode()).hexdigest()[:16]
    return out


# ------------------------------------------------------------------ текст
def period_facts(s: Dict[str, Any]) -> List[str]:
    """Факты сводки периода готовыми фразами (для модели и шаблона), без кодов рисков."""
    from risk_monitoring.daily import _sc, risk_names

    facts: List[str] = []
    dd = s["days"]
    facts.append(f"Сводка за {s['period']['title']}: ежедневных сводок {len(dd['completed'])} из "
                 f"{dd['total'] - len(dd['future'])} прошедших дней.")
    if dd["missing"] or dd["partial"]:
        facts.append("Сводка неполная: " + "; ".join(
            x for x in (f"нет сводки за {', '.join(d[8:10] + '.' + d[5:7] for d in dd['missing'])}" if dd["missing"] else "",
                        f"сводка не завершена за {', '.join(d[8:10] + '.' + d[5:7] for d in dd['partial'])}"
                        if dd["partial"] else "") if x) + ".")
    ev = s["events"]
    if ev["total"]:
        facts.append(f"Событий ЕИС по закупкам системы: {ev['total']}; чаще всего — " + ", ".join(
            f"{k.lower()} ({v})" for k, v in list(ev["by_type"].items())[:4]) + ".")
    c = s["contracts"]
    facts.append(f"Пересчитано закупок (с повторами по дням): {c['recalculated']}; перешли в красную зону или "
                 f"оказались в ней при первой оценке — {c['became_red']}.")
    if c["became_red_list"]:
        facts.append("В красной зоне: " + "; ".join(
            f"закупка {i.get('purchase_number')}, {i.get('customer')} — балл {_sc(i.get('score'))}"
            for i in c["became_red_list"][:5]) + ".")
    rc = s["risk_changes"]
    if rc["new_total"] or rc["closed_total"]:
        facts.append(f"Новых рисков в закупках: {rc['new_total']}; снято: {rc['closed_total']}.")
    if s.get("composite_risks"):
        facts.append("Сочетания признаков риска (упоминаний за период): " + "; ".join(
            f"{risk_names([k])} — {v}" for k, v in list(s["composite_risks"].items())[:5]) + ".")
    pr = [x for x in s.get("priorities") or [] if x.get("priority")]
    if pr:
        facts.append("Проверить в первую очередь: " + "; ".join(
            f"закупка {x.get('purchase_number')}, {x.get('customer')} — приоритет {_sc(x.get('priority'))}"
            + (f" ({', '.join(x['priority_reasons'])})" if x.get("priority_reasons") else "") for x in pr[:5]) + ".")
    cu = [x for x in s.get("customers") or [] if x.get("zone") in ("red", "yellow")]
    if cu:
        facts.append("Заказчики с высоким баллом: " + "; ".join(
            f"{x.get('name')} — {_sc(x.get('score'))}" for x in cu[:5]) + ".")
    if s.get("execution_open_past_plan"):
        facts.append(f"На конец периода плановый срок исполнения прошёл без сведений о завершении — контрактов "
                     f"{s['execution_open_past_plan']}.")
    cmp_ = s.get("comparison")
    if cmp_:
        def sign(v: int) -> str:
            """Изменение со знаком словами."""
            return f"больше на {v}" if v > 0 else f"меньше на {-v}" if v < 0 else "столько же"
        facts.append(f"По сравнению с прошлым периодом: событий {sign(cmp_['events'])}, закупок в красной зоне "
                     f"{sign(cmp_['became_red'])}, новых рисков {sign(cmp_['new_risks'])}.")
    return facts


def default_resume(s: Dict[str, Any]) -> str:
    """Текст сводки периода шаблоном: факты подряд."""
    return " ".join(period_facts(s))


# ------------------------------------------------------------------ прогон
async def run_period(repo: Any, environment: str, kind: str, period: str, use_llm: bool = True,
                     force: bool = False, today: Optional[date] = None) -> Dict[str, Any]:
    """Собирает и сохраняет сводку периода (``ai_rm_results``, ``kind = weekly / monthly``).

    Готовая сводка с теми же входными данными (ежедневные сводки не изменились) не пересобирается — модель второй
    раз не вызывается (кроме ``force``).

    Args:
        repo: ``AIRepo``.
        environment: Окружение.
        kind: ``week`` / ``month``.
        period: Обозначение периода.
        use_llm: Текст моделью.
        force: Пересобрать в любом случае.
        today: Сегодня.

    Returns:
        Dict[str, Any]: Сводка периода (``status`` в ``pipeline``: ``completed`` / ``partial``; ``unchanged`` —
        возвращена сохранённая).
    """
    from risk_monitoring import daily, passport

    env = environment.lower()
    ds = days(kind, period)
    keys = {daily.run_key(d): d for d in ds}
    stored = await repo.get_rm_results(env, daily.KIND, list(keys))
    dailies = {keys[k]: v["ai_analysis"] for k, v in stored.items() if v.get("ai_analysis")}
    pkey, prev_key = period_key(kind, period), period_key(kind, previous_period(kind, period))
    have = await repo.get_rm_results(env, KINDS[kind], [pkey, prev_key])
    prev = (have.get(prev_key) or {}).get("ai_analysis")
    s = build_period_summary(kind, period, dailies, prev, today)
    old = (have.get(pkey) or {}).get("ai_analysis") or {}
    if not force and old.get("input_hash") == s["input_hash"] \
            and (old.get("pipeline") or {}).get("schema_version") == SCHEMA_VERSION:
        return {**old, "unchanged": True}
    resume, used = default_resume(s), False
    if use_llm and settings.PACKAGE_LLM_RESUME and dailies:
        try:
            from configs.llm_client import get_llm
            from risk_monitoring.llm_json import LLMJson, reasoning_llm

            client, model = reasoning_llm(*get_llm())
            text = await LLMJson(client, model, concurrency=1).ask_text(
                RESUME_SYSTEM.format(period=s["period"]["title"]),
                json.dumps({"facts": period_facts(s)}, ensure_ascii=False), max_tokens=1800, temperature=0.2)
            resume, used = (text, True) if text else (resume, False)
        except Exception as e:  # noqa: BLE001 — текст не обязателен
            logger.warning(f"Сводка за {period} шаблоном: {type(e).__name__}: {e}")
    s["resume"] = resume
    s["facts"] = period_facts(s)
    incomplete = s["days"]["missing"] or s["days"]["partial"] or s["days"]["future"]
    s["pipeline"] = {"schema_version": SCHEMA_VERSION, "stage": "S5", "status": "partial" if incomplete else "completed",
                     "finished_at": passport.now_iso(),
                     "prompt_versions": {"period_resume": PROMPT_VERSION} if used else {}}
    await repo.save_rm_result(env, KINDS[kind], pkey, s, False, None)
    logger.info(f"Сводка за {s['period']['title']} ({env}): дней {len(s['days']['completed'])}/{s['days']['total']}, "
                f"статус {s['pipeline']['status']}")
    return s

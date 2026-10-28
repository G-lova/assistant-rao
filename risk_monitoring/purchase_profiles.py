"""Профили закупки: исполнение контракта и сравнение с похожими закупками системы.

- :func:`execution_profile` — показатели исполнения по данным ЕИС (срок по плану и факту, просрочка, доля
  исполнения и оплаты, задержка приёмки, изменения, расторжение) и риски по ним (:func:`execution_risks`).
- :func:`benchmark` — снижение цены и число заявок закупки против похожих (тот же класс ОКПД2 и закон, конкурентный
  способ); аномально малое снижение там, где по похожим закупкам цену обычно снижают заметно, — признак
  завышенной НМЦК (FIN-001, :func:`benchmark_risks`).

Функции чистые: данные внешней БД передаются аргументами.
"""
import re
import statistics
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from risk_monitoring import risk_catalog

OKPD_RE = re.compile(r"^\s*(\d{2})\.(\d{2})")


def _g(d: Dict[str, Any], *path: str) -> Any:
    """Значение по пути в ``dataset`` (``None`` — нет)."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return None if d in ("", "null") else d


def _f(v: Any) -> Optional[float]:
    """Число из строки ЕИС (``None`` — не число)."""
    try:
        return None if v is None or v == "" else float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _d(v: Any) -> Optional[date]:
    """Дата из строки ЕИС («2026-12-31+03:00», «2026-06-17 00:00:00», «2026-06-17T16:32:20»)."""
    s = str(v or "")[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _ru(x: float) -> str:
    """Число для текста: до одного знака после запятой, запятая — разделитель."""
    return f"{round(x, 1):g}".replace(".", ",")


def _flag(v: Any) -> Optional[bool]:
    """Логическое значение ЕИС (``true`` / ``false``)."""
    s = str(v).strip().lower() if v is not None else ""
    return True if s in ("true", "1") else False if s in ("false", "0") else None


# ------------------------------------------------------------------ исполнение контракта
def execution_profile(dataset: Dict[str, Any], today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """Показатели исполнения контракта по данным ЕИС.

    Args:
        dataset: ``dataset`` карточки.
        today: Текущая дата (для «срок по плану прошёл, исполнение не завершено»).

    Returns:
        Optional[Dict[str, Any]]: ``None`` — контракта ещё нет. Иначе: ``status``, ``price``, ``plan_end``,
        ``fact_end``, ``plan_days``, ``fact_days``, ``delay_days``, ``executed``, ``paid``, ``executed_share``,
        ``paid_share``, ``acceptance_lag_days``, ``stages``, ``modified``, ``terminated``, ``completed``,
        ``plan_passed_open``, ``notes``.
    """
    today = today or datetime.now(timezone.utc).date()
    price = _f(_g(dataset, "ispolnenie", "contr_zena"))
    number = _g(dataset, "contract_details", "num_contr")
    if price is None and not number:
        return None
    status = _g(dataset, "contract_details", "contr_status")
    stages = int(_f(_g(dataset, "contract", "stages_count")) or 0) or None
    plan_end, fact_end = _d(_g(dataset, "ispolnenie", "d_contr_plan")), _d(_g(dataset, "ispolnenie", "d_contr_fact"))
    plan_days = _f(_g(dataset, "ispolnenie", "contr_srok_ispoln_days"))
    fact_days = _f(_g(dataset, "ispolnenie", "contr_srok_fact_days"))
    delay = None
    # фактический срок у многоэтапного контракта — срок этапа: сравнивать с плановым сроком контракта нельзя
    single = not stages or stages <= 1
    if single and plan_days is not None and fact_days is not None:
        delay = int(fact_days - plan_days)
    elif single and plan_end and fact_end:
        delay = (fact_end - plan_end).days
    executed = _f(_g(dataset, "ispolnenie", "contr_zena_ispoln")) or _f(_g(dataset, "procedure", "fulfilment_sum"))
    paid = _f(_g(dataset, "procedure", "paid_sum"))
    if paid is None:
        paid = _f(_g(dataset, "ispolnenie", "contr_zena_viplat"))
    accept, stage_end = _d(_g(dataset, "procedure", "delivery_accept_dt")), _d(_g(dataset, "procedure", "stage_end"))
    st = str(status or "").lower()
    terminated = bool(_g(dataset, "procedure", "term_code")) or "прекращ" in st
    # procedure.* в dataset — сведения об одном (последнем) этапе: final_stage = true значит «этап завершён», а не
    # контракт (28.10.2026: у многоэтапных контрактов выходило «завершён на 50 %»). Завершение — только по статусу
    completed = "заверш" in st
    share = (lambda x: round(x / price, 3) if x is not None and price else None)
    out = {
        "status": status, "price": price,
        "plan_end": plan_end.isoformat() if plan_end else None, "fact_end": fact_end.isoformat() if fact_end else None,
        "plan_days": int(plan_days) if plan_days is not None else None,
        "fact_days": int(fact_days) if fact_days is not None else None,
        "delay_days": delay if delay is not None and delay > 0 else 0 if delay is not None else None,
        "executed": executed, "paid": paid, "executed_share": share(executed), "paid_share": share(paid),
        "acceptance_lag_days": (accept - stage_end).days if accept and stage_end and accept > stage_end else
        (0 if accept and stage_end else None),
        "stages": stages,
        "modified": bool(_g(dataset, "contract", "mod_reason_code")),
        "terminated": terminated, "completed": bool(completed),
        "plan_passed_open": bool(plan_end and plan_end < today and not completed and not terminated and fact_end is None),
        "notes": [],
    }
    if out["plan_passed_open"]:
        out["notes"].append(f"плановый срок {out['plan_end']} прошёл, сведений о завершении исполнения в ЕИС нет")
    if out["delay_days"]:
        out["notes"].append(f"исполнено с опозданием на {out['delay_days']} дн.")
    if out["acceptance_lag_days"]:
        out["notes"].append(f"приёмка позже окончания этапа на {out['acceptance_lag_days']} дн.")
    return out


def execution_risks(ex: Optional[Dict[str, Any]], cfg: Any) -> List[Dict[str, Any]]:
    """Риски исполнения по профилю: фактическая просрочка (CTR-017) и недоисполнение завершённого контракта
    (CTR-018). «Срок прошёл, сведений нет» — не риск: данные исполнения в ЕИС часто запаздывают, это пометка
    для проверки.

    Args:
        ex: :func:`execution_profile`.
        cfg: Настройки (``EXECUTION_DELAY_LEVELS``, ``EXECUTION_UNDERDONE_SHARE``).

    Returns:
        List[Dict[str, Any]]: Риски во внешнем формате (``source = data``).
    """
    if not ex:
        return []
    out = []
    d = ex.get("delay_days") or 0
    if d > 0:
        level = next((lv for days, lv in cfg.EXECUTION_DELAY_LEVELS if d <= days), cfg.EXECUTION_DELAY_LEVELS[-1][1])
        out.append({"rule_id": "CTR-017", "title": risk_catalog.title("CTR-017"),
                    "description": f"Контракт исполнен с опозданием на {d} дн. (план {ex.get('plan_days')} дн., "
                                   f"факт {ex.get('fact_days')} дн.)",
                    "level": level, "confidence": 1.0, "law": None,
                    "evidence": [{"src": "eis", "path": "ispolnenie.contr_srok_fact_days", "value": ex.get("fact_days")},
                                 {"src": "eis", "path": "ispolnenie.contr_srok_ispoln_days", "value": ex.get("plan_days")}],
                    "verification_needed": ["Проверить, предъявлены ли поставщику пени за просрочку (ст. 34 44-ФЗ)"],
                    "source": "data"})
    es = ex.get("executed_share")
    if ex.get("completed") and es is not None and es < cfg.EXECUTION_UNDERDONE_SHARE:
        out.append({"rule_id": "CTR-018", "title": risk_catalog.title("CTR-018"),
                    "description": f"Исполнение завершено на {round(es * 100)} % цены контракта",
                    "level": 0.4, "confidence": 0.9, "law": None,
                    "evidence": [{"src": "eis", "path": "ispolnenie.contr_zena_ispoln", "value": ex.get("executed")}],
                    "verification_needed": ["Проверить основание неполного исполнения и уменьшения оплаты"],
                    "source": "data"})
    return out


# ------------------------------------------------------------------ сравнение с похожими закупками
def okpd_class(code: Any, digits: int = 4) -> Optional[str]:
    """Класс ОКПД2: 4 знака (``32.50``) или раздел — 2 знака (``32``)."""
    m = OKPD_RE.match(str(code or ""))
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}" if digits == 4 else m.group(1)


def _pct(values: List[float], q: float) -> float:
    """Перцентиль (линейная интерполяция)."""
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def benchmark(passport: Dict[str, Any], peers: Sequence[Dict[str, Any]], cfg: Any) -> Optional[Dict[str, Any]]:
    """Снижение цены и конкуренция закупки против похожих закупок системы.

    Похожие — тот же класс ОКПД2 (4 знака) и закон, конкурентный способ, снижение 0–100 %; если их меньше
    ``BENCHMARK_MIN_PEERS`` — раздел ОКПД2 (2 знака), сравнение помечается как грубое.

    Args:
        passport: Паспорт закупки (``okpd2``, ``fz``, ``card_id``, ``winner_discount_pct``, ``applications``).
        peers: Строки ``benchmark_peers.sql``.
        cfg: Настройки (``BENCHMARK_MIN_PEERS``).

    Returns:
        Optional[Dict[str, Any]]: ``None`` — сравнивать не с чем. Иначе: ``group`` (``class`` / ``division``),
        ``okpd2``, ``peers``, ``discount`` (``value``, ``median``, ``p10``, ``p90``, ``rank_pct``),
        ``applications`` (``value``, ``median``).
    """
    from risk_monitoring.contract_package import competitive

    own_cls = okpd_class(passport.get("okpd2"))
    if not own_cls:
        return None
    own = passport.get("winner_discount_pct")
    if own is None:
        own = passport.get("price_reduction_pct")
    fz = passport.get("fz")

    def group(digits: int) -> List[Dict[str, Any]]:
        """Похожие закупки по классу или разделу."""
        key = okpd_class(passport.get("okpd2"), digits)
        rows = []
        for r in peers:
            if r.get("id") is not None and passport.get("card_id") is not None and int(r["id"]) == int(passport["card_id"]):
                continue
            if okpd_class(r.get("okpd2"), digits) != key:
                continue
            if fz and risk_catalog.normalize_fz(r.get("fz_type")) not in (None, fz):
                continue
            if competitive(r.get("method_code"), r.get("method")) is False:
                continue
            d = _f(r.get("discount"))
            if d is None or not 0 <= d <= 100:
                continue
            rows.append({"discount": d, "applications": _f(r.get("applications"))})
        return rows

    kind, rows = "class", group(4)
    if len(rows) < cfg.BENCHMARK_MIN_PEERS:
        kind, rows = "division", group(2)
    if len(rows) < cfg.BENCHMARK_MIN_PEERS:
        return None
    ds = [r["discount"] for r in rows]
    apps = [r["applications"] for r in rows if r["applications"] is not None]
    return {
        "group": kind, "okpd2": own_cls if kind == "class" else okpd_class(passport.get("okpd2"), 2), "peers": len(rows),
        "discount": {"value": own, "median": round(statistics.median(ds), 2), "p10": round(_pct(ds, 0.1), 2),
                     "p90": round(_pct(ds, 0.9), 2),
                     "rank_pct": round(100 * sum(1 for d in ds if d < own) / len(ds), 1) if own is not None else None},
        "applications": {"value": passport.get("applications"),
                         "median": statistics.median(apps) if apps else None},
    }


def benchmark_risks(b: Optional[Dict[str, Any]], passport: Dict[str, Any], cfg: Any) -> List[Dict[str, Any]]:
    """Аномально малое снижение цены относительно похожих закупок → FIN-001 «Аномальная цена».

    Условие: конкурентный способ, не меньше двух заявок; снижение не выше 10-го перцентиля похожих и меньше медианы не менее чем на
    ``BENCHMARK_LOW_DISCOUNT_GAP`` п. п.; медиана похожих не меньше ``BENCHMARK_MIN_MEDIAN`` %. Высокое снижение
    не повторяется — это демпинг (FIN-003, правило внешней системы).

    Returns:
        List[Dict[str, Any]]: Риск во внешнем формате (``source = benchmark``) или пусто.
    """
    from risk_monitoring.contract_package import competitive

    if not b or competitive(passport.get("method_code"), passport.get("method")) is not True:
        return []
    d = b["discount"]
    own, med = d.get("value"), d.get("median")
    if own is None or med is None or med < cfg.BENCHMARK_MIN_MEDIAN or not 0 <= own <= 100:
        return []
    # одна заявка — контракт по НМЦК: это уже «низкая конкуренция» (PROC-001), второй риск по тому же факту
    # не нужен (на данных 28.10.2026: 108 из 127 срабатываний — одна заявка)
    apps = passport.get("applications")
    if apps is None or apps < 2:
        return []
    if not (own <= d["p10"] and med - own >= cfg.BENCHMARK_LOW_DISCOUNT_GAP):
        return []
    rough = b["group"] == "division"
    where = f"раздел ОКПД2 {b['okpd2']}" if rough else f"класс ОКПД2 {b['okpd2']}"
    return [{"rule_id": "FIN-001", "title": risk_catalog.title("FIN-001"),
             "description": f"Снижение цены {_ru(own)} % при {int(apps)} заявках, а у {b['peers']} похожих закупок "
                            f"системы ({where}) медиана снижения {_ru(med)} %: возможно, начальная цена завышена "
                            f"или участники не конкурировали",
             "level": cfg.BENCHMARK_RISK_LEVEL * (0.75 if rough else 1.0), "confidence": 0.7, "law": None,
             "evidence": [{"src": "benchmark", "group": b["group"], "okpd2": b["okpd2"], "peers": b["peers"],
                           "value": own, "median": med, "p10": d["p10"]}],
             "verification_needed": ["Проверить обоснование НМЦК (ст. 22 44-ФЗ): источники цен и их актуальность",
                                     "Сравнить цену за единицу с похожими закупками"],
             "source": "benchmark"}]

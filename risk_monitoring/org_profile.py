"""Аналитика организации как заказчика и как поставщика (этапы S3b–S3c): метрики за период, риски, балл.

Чистые функции; данные собирает :class:`risk_monitoring.org_analyzer.OrgAnalyzer`.

Ключ агрегации: заказчик — пара ИНН/КПП (все строки ``risk_monitoring_organisations`` с этой парой, по годам и
дублям); поставщик — ИНН (филиалы вместе). Результат пишется в каноническую строку
(``xml_events.canonical_org``).

Поведенческие риски ставятся, только если закупок за период не меньше ``settings.ORG_MIN_CONTRACTS``:
на малой выборке доли случайны.

| Роль | Код | Условие |
|---|---|---|
| заказчик | BEH-001 | доля закупок у единственного поставщика или с одной заявкой ≥ ``CUSTOMER_LOW_COMPETITION_SHARE`` |
| заказчик | BEH-006 | «любимый поставщик»: доля контрактов у одного поставщика ≥ ``CUSTOMER_TOP_SUPPLIER_SHARE`` (и не меньше 3 контрактов) |
| заказчик | BEH-005 | концентрация сумм контрактов: HHI ≥ ``CUSTOMER_HHI`` |
| заказчик | HIST-001 | один и тот же риск в ``REPEAT_RISK_MIN`` и более закупках |
| заказчик | REG-001 | контракты с поставщиками из реестра 19.28 КоАП на дату контракта |
| поставщик | REG-001 | действующая запись в реестре 19.28 КоАП |
| поставщик | BEH-002 | доля расторгнутых контрактов ≥ ``SUPPLIER_TERMINATION_SHARE`` или доля демпинга ≥ 50 % |
"""
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from risk_monitoring import contract_package as cp
from risk_monitoring import passport, risk_catalog

SCHEMA_VERSION = "rm-org-1"


def _risk(code: str, description: str, level: float, evidence: List[Dict[str, Any]],
          verification: List[str], confidence: float = 0.8, source: Optional[str] = None) -> Dict[str, Any]:
    """Риск аналитики во внешнем формате (``source``: ``aggregate``, для REG-* — ``registry``)."""
    return {"rule_id": code, "title": risk_catalog.title(code), "description": description, "level": level,
            "confidence": confidence, "law": None, "evidence": evidence, "verification_needed": verification,
            "source": source or ("registry" if code.startswith("REG-") else "aggregate")}


def _share(part: int, total: int) -> Optional[float]:
    """Доля с округлением; ``None`` при пустом знаменателе."""
    return round(part / total, 3) if total else None


def _competitive(c: Dict[str, Any]) -> Optional[bool]:
    """Конкурентный способ закупки (``None`` — способ неизвестен)."""
    return cp.competitive(c.get("method_code"), c.get("method"))


def _price(c: Dict[str, Any]) -> float:
    """Цена контракта, иначе НМЦК, иначе 0."""
    return cp.num(c.get("contract_price")) or cp.num(c.get("nmck")) or 0.0


def _top(contracts: Sequence[Dict[str, Any]], inn_key: str, name_key: str, n: int = 5) -> List[Dict[str, Any]]:
    """Крупнейшие контрагенты: число контрактов, сумма, доля по числу."""
    by: Dict[str, Dict[str, Any]] = {}
    for c in contracts:
        inn = str(c.get(inn_key) or "").strip()
        if not inn:
            continue
        b = by.setdefault(inn, {"inn": inn, "name": c.get(name_key), "contracts": 0, "sum": 0.0})
        b["contracts"] += 1
        b["sum"] += _price(c)
    total = sum(b["contracts"] for b in by.values())
    out = sorted(by.values(), key=lambda b: (b["contracts"], b["sum"]), reverse=True)[:n]
    for b in out:
        b["share"] = _share(b["contracts"], total)
        b["sum"] = round(b["sum"], 2)
    return out


def _hhi(contracts: Sequence[Dict[str, Any]], inn_key: str) -> Optional[float]:
    """Индекс Херфиндаля–Хиршмана по сумме контрактов (0–10 000)."""
    sums: Dict[str, float] = defaultdict(float)
    for c in contracts:
        inn = str(c.get(inn_key) or "").strip()
        if inn:
            sums[inn] += _price(c)
    total = sum(sums.values())
    return round(sum((v / total * 100) ** 2 for v in sums.values()), 1) if total else None


def common_metrics(contracts: Sequence[Dict[str, Any]], demping_pct: float) -> Dict[str, Any]:
    """Метрики, общие для обеих ролей: объёмы, конкуренция, демпинг, изменения, расторжения."""
    n = len(contracts)
    comp = [c for c in contracts if _competitive(c)]
    noncomp = [c for c in contracts if _competitive(c) is False]
    comp_apps = [c for c in comp if cp.num(c.get("app_count")) is not None or cp.flag(c.get("single_app_flag")) is not None]
    single_app = [c for c in comp_apps if cp.flag(c.get("single_app_flag")) or cp.num(c.get("app_count")) == 1]
    disc = [cp.num(c.get("win_discount_pct")) for c in contracts if cp.num(c.get("win_discount_pct")) is not None]
    scores = [cp.num(c.get("score")) for c in contracts if cp.num(c.get("score")) is not None]
    return {
        "contracts": n,
        "with_contract": sum(1 for c in contracts if c.get("contract_number")),
        "nmck_sum": round(sum(cp.num(c.get("nmck")) or 0 for c in contracts), 2),
        "nmck_missing": sum(1 for c in contracts if cp.num(c.get("nmck")) is None),
        "contract_sum": round(sum(cp.num(c.get("contract_price")) or 0 for c in contracts), 2),
        "noncompetitive": len(noncomp),
        "noncompetitive_share": _share(len(noncomp), len(comp) + len(noncomp)),
        "method_unknown": n - len(comp) - len(noncomp),
        "single_application": len(single_app),
        "single_application_share": _share(len(single_app), len(comp_apps)),
        "avg_discount_pct": round(sum(disc) / len(disc), 2) if disc else None,
        "demping": sum(1 for d in disc if d >= demping_pct),
        "demping_share": _share(sum(1 for d in disc if d >= demping_pct), len(disc)),
        "modified": sum(1 for c in contracts if c.get("mod_reason_code")),
        "modified_share": _share(sum(1 for c in contracts if c.get("mod_reason_code")), n),
        "terminated": sum(1 for c in contracts if c.get("term_code")),
        "terminated_share": _share(sum(1 for c in contracts if c.get("term_code")), n),
        "penalties": sum(1 for c in contracts if cp.flag(c.get("penalty_fact"))),
        "analyzed": len(scores),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "red_zone": sum(1 for c in contracts if c.get("zone") == "red"),
        "red_zone_share": _share(sum(1 for c in contracts if c.get("zone") == "red"), len(scores)),
    }


def _risk_codes(contracts: Sequence[Dict[str, Any]]) -> Counter:
    """Сколько закупок содержат риск каждого кода (по ``contracts.ai_analysis.risks``)."""
    cnt: Counter = Counter()
    for c in contracts:
        for code in set(cp.as_json(c.get("risk_codes")) or []):
            if code:
                cnt[code] += 1
    return cnt


def _black_on(contract: Dict[str, Any], black: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Записи реестра 19.28, действующие для поставщика контракта на дату контракта."""
    return cp.registry_matches(black, {"inn": contract.get("supplier_inn"), "kpp": contract.get("supplier_kpp")},
                               cp.as_date(contract.get("contract_date")) or cp.as_date(contract.get("date_public")))


def customer_section(contracts: Sequence[Dict[str, Any]], black: Sequence[Dict[str, Any]], settings: Any,
                     previous_score: Optional[float] = None) -> Dict[str, Any]:
    """Секция ``customer``: метрики, риски, балл заказчика.

    Args:
        contracts: Закупки заказчика за период.
        black: Записи реестра 19.28 по ИНН его поставщиков.
        settings: Пороги (``configs.settings``).
        previous_score: Прошлый балл.

    Returns:
        Dict[str, Any]: ``metrics``, ``top_suppliers``, ``risks``, ``score``, ``zone``, ``delta``, ``top_factors``.
    """
    m = common_metrics(contracts, settings.DEMPING_PCT)
    with_supplier = [c for c in contracts if c.get("supplier_inn")]
    top = _top(with_supplier, "supplier_inn", "supplier_name")
    m["suppliers"] = len({str(c["supplier_inn"]).strip() for c in with_supplier})
    m["with_supplier"] = len(with_supplier)
    m["hhi"] = _hhi(with_supplier, "supplier_inn")
    m["top_supplier_share"] = top[0]["share"] if top else None
    repeated = {code: n for code, n in _risk_codes(contracts).items() if n >= settings.REPEAT_RISK_MIN}
    m["repeated_risks"] = dict(sorted(repeated.items(), key=lambda kv: -kv[1]))
    black_contracts = [(c, b) for c in with_supplier for b in _black_on(c, black)]
    m["registry_1928_contracts"] = len({c["id"] for c, _ in black_contracts})

    risks: List[Dict[str, Any]] = []
    enough = m["contracts"] >= settings.ORG_MIN_CONTRACTS
    low = max(m["noncompetitive_share"] or 0, m["single_application_share"] or 0)
    if enough and low >= settings.CUSTOMER_LOW_COMPETITION_SHARE:
        risks.append(_risk("BEH-001", f"Низкая конкуренция в закупках заказчика: у единственного поставщика "
                                      f"{m['noncompetitive']} из {m['contracts']}, с одной заявкой {m['single_application']}",
                           0.5, [{"src": "aggregate", "noncompetitive_share": m["noncompetitive_share"],
                                  "single_application_share": m["single_application_share"]}],
                           ["Проверить основания закупок у единственного поставщика и требования документации"]))
    if len(with_supplier) >= settings.ORG_MIN_CONTRACTS and top and top[0]["contracts"] >= 3 \
            and (top[0]["share"] or 0) >= settings.CUSTOMER_TOP_SUPPLIER_SHARE:
        t = top[0]
        risks.append(_risk("BEH-006", f"«Любимый поставщик»: {t['name'] or t['inn']} — {t['contracts']} из "
                                      f"{len(with_supplier)} контрактов заказчика ({round(100 * t['share'])} %)",
                           0.6, [{"src": "aggregate", "supplier_inn": t["inn"], "share": t["share"], "hhi": m["hhi"]}],
                           ["Проверить конкуренцию в закупках, где побеждает этот поставщик, связи сторон и дробление закупок"]))
    if len(with_supplier) >= settings.ORG_MIN_CONTRACTS and m["suppliers"] >= 2 and (m["hhi"] or 0) >= settings.CUSTOMER_HHI:
        risks.append(_risk("BEH-005", f"Высокая концентрация сумм контрактов заказчика (HHI {m['hhi']:.0f})",
                           0.4, [{"src": "aggregate", "hhi": m["hhi"], "suppliers": m["suppliers"]}],
                           ["Проверить, не распределяются ли крупные контракты между узким кругом поставщиков"]))
    if repeated:
        top_codes = list(m["repeated_risks"].items())[:5]
        risks.append(_risk("HIST-001", "Повторяющиеся риски в закупках заказчика: " +
                           ", ".join(f"{c} {risk_catalog.title(c)} — {n}" for c, n in top_codes),
                           0.4, [{"src": "aggregate", "codes": dict(top_codes)}],
                           ["Проверить, не воспроизводится ли один и тот же шаблон документации"], confidence=0.9))
    if black_contracts:
        risks.append(_risk("REG-001", f"Контракты с поставщиками из реестра 19.28 КоАП: {m['registry_1928_contracts']}",
                           0.8, [{"src": f"contract:{c['id']}", "black_list_id": b.get("id"), "supplier_inn": c.get("supplier_inn")}
                                 for c, b in black_contracts[:5]],
                           ["Проверить даты заключения контрактов и действие записи реестра"], confidence=1.0))
    return _finish(m, risks, previous_score, settings, top_suppliers=top)


def supplier_section(contracts: Sequence[Dict[str, Any]], black: Sequence[Dict[str, Any]], inn: str, settings: Any,
                     previous_score: Optional[float] = None, today: Optional[date] = None,
                     rnp: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Секция ``supplier``: метрики, риски, балл поставщика.

    Args:
        contracts: Контракты поставщика за период.
        black: Записи реестра 19.28 по ИНН поставщика.
        inn: ИНН поставщика.
        settings: Пороги.
        previous_score: Прошлый балл.
        today: Дата проверки реестра (по умолчанию — сегодня).
        rnp: Записи РНП по ИНН поставщика → REG-002 (действующие на сегодня).

    Returns:
        Dict[str, Any]: Как у :func:`customer_section`, с ``top_customers`` и ``registry``.
    """
    m = common_metrics(contracts, settings.DEMPING_PCT)
    top = _top(contracts, "customer_inn", "customer_name")
    m["customers"] = len({str(c.get("customer_inn")).strip() for c in contracts if c.get("customer_inn")})
    m["with_customer"] = sum(1 for c in contracts if c.get("customer_inn"))
    m["top_customer_share"] = top[0]["share"] if top else None
    m["smp"] = next((c.get("supplier_status") for c in contracts if c.get("supplier_status")), None)
    active = cp.registry_matches(black, {"inn": inn}, today)

    risks: List[Dict[str, Any]] = []
    if active:
        risks.append(_risk("REG-001", f"Поставщик в реестре лиц, привлечённых по ст. 19.28 КоАП РФ "
                                      f"(с {str(active[0].get('date_begin'))[:10]})", 0.9,
                           [{"src": f"black_list:{b.get('id')}", "deal_number": b.get("deal_number"),
                             "date_begin": str(b.get("date_begin"))[:10]} for b in active],
                           ["Проверить, не заключались ли контракты в период действия записи"], confidence=1.0))
    rnp_active = cp.rnp_matches(rnp, {"inn": inn}, today)
    if rnp_active:
        e = rnp_active[0]
        risks.append(_risk("REG-002", f"Поставщик в реестре недобросовестных поставщиков (запись {e.get('reg_number')}, "
                                      f"с {str(e.get('first_published_at') or e.get('published_at') or '')[:10]})", 0.8,
                           [{"src": f"rnp:{x.get('id')}", "reg_number": x.get("reg_number"), "fz": x.get("fz")}
                            for x in rnp_active],
                           ["Проверить контракты, заключённые в период включения в РНП"], confidence=1.0))
    enough = m["contracts"] >= settings.ORG_MIN_CONTRACTS
    # «Любимый поставщик» (BEH-006) — поведение заказчика, считается в его секции; здесь только метрика
    # top_customer_share (в БД — закупки заказчиков на мониторинге, поэтому у поставщика она завышена)
    if m["contracts"] >= 3 and (m["terminated_share"] or 0) >= settings.SUPPLIER_TERMINATION_SHARE:
        risks.append(_risk("BEH-002", f"Расторгнуто {m['terminated']} из {m['contracts']} контрактов поставщика",
                           0.6, [{"src": "aggregate", "terminated_share": m["terminated_share"]}],
                           ["Проверить причины расторжений и наличие записей в РНП"]))
    if enough and (m["demping_share"] or 0) >= 0.5:
        risks.append(_risk("BEH-002", f"Систематический демпинг: снижение ≥ {settings.DEMPING_PCT} % в {m['demping']} "
                                      f"контрактах", 0.4, [{"src": "aggregate", "demping_share": m["demping_share"]}],
                           ["Проверить исполнение демпинговых контрактов и антидемпинговые меры"]))
    return _finish(m, risks, previous_score, settings, top_customers=top,
                   registry=[{"black_list_id": b.get("id"), "deal_number": b.get("deal_number"),
                              "date_begin": str(b.get("date_begin"))[:10]} for b in active],
                   rnp=[{"rnp_entry_id": e.get("id"), "reg_number": e.get("reg_number"), "fz": e.get("fz"),
                         "published_at": str(e.get("published_at") or "")[:10]} for e in rnp_active])


def _finish(metrics: Dict[str, Any], risks: List[Dict[str, Any]], previous_score: Optional[float], settings: Any,
            **extra: Any) -> Dict[str, Any]:
    """Объединение рисков, балл и зона секции."""
    merged = passport.merge_risks(risks)
    prof = cp.profile(merged, previous_score, (), settings.CONFIRMED_WEIGHT, tuple(settings.SCORE_ZONES),
                      settings.SCORE_DECAY, settings.AI_UNCONFIRMED_WEIGHT)
    return {"metrics": metrics, **extra, "risks": merged, "score": prof["score"], "zone": prof["zone"],
            "delta": prof["delta"], "previous_score": previous_score, "components": prof["components"],
            "top_factors": prof["top_factors"]}


def _pct(share: Optional[float]) -> str:
    """Доля в процентах без лишних знаков: 0.333 → «33 %»."""
    return f"{round(100 * (share or 0))} %"


def resume_facts(role: str, section: Dict[str, Any], settings: Any) -> List[str]:
    """Факты секции готовыми фразами — из них модель пишет резюме, ничего не вычисляя сама.

    По сырым метрикам модель путала смысл: «способ закупки не указан» — как «закупка без конкурса», три поставщика
    с равной долей — как «доминирует первый», а при трёх контрактах делала выводы о концентрации (20.10.2026).

    Args:
        role: ``customer`` / ``supplier``.
        section: Секция (``metrics``, ``top_suppliers`` / ``top_customers``, ``risks``, ``score``).
        settings: Пороги (``ORG_MIN_CONTRACTS``).

    Returns:
        List[str]: Фразы в порядке: объём, конкуренция, цены, исполнение, контрагенты, риски, балл.
    """
    m = section["metrics"]
    n = m["contracts"]
    facts = [f"Закупок за период: {n}, из них с заключённым контрактом: {m['with_contract']}; сумма контрактов "
             f"{cp.money(m['contract_sum'])} руб., сумма НМЦК {cp.money(m['nmck_sum'])} руб"
             + (f" (закупок без НМЦК в данных: {m['nmck_missing']} — суммы не сопоставимы)" if m.get("nmck_missing") else "")
             + "."]
    known = n - m["method_unknown"]
    if known:
        facts.append("Все закупки с известным способом — конкурентные." if not m["noncompetitive"] else
                     f"У единственного поставщика: {m['noncompetitive']} из {known} закупок с известным способом.")
    if m["method_unknown"]:
        facts.append(f"Закупок, у которых способ в данных не указан: {m['method_unknown']} (это не означает закупку "
                     "без конкурса).")
    if m["single_application_share"] is not None:
        facts.append(f"Конкурентных закупок с одной заявкой: {m['single_application']} ({_pct(m['single_application_share'])}).")
    if m["avg_discount_pct"] is not None:
        facts.append(f"Среднее снижение цены победителем {m['avg_discount_pct']} %; снижение ≥ {settings.DEMPING_PCT} % "
                     f"(антидемпинговые меры) — закупок: {m['demping']} из тех, где снижение известно.")
    if m["modified"] or m["terminated"]:
        facts.append(f"Контрактов изменено: {m['modified']}, расторгнуто: {m['terminated']}.")
    if m["penalties"]:
        facts.append(f"Контрактов с начисленными неустойками: {m['penalties']}.")
    top = section.get("top_suppliers") if role == "customer" else section.get("top_customers")
    linked = m.get("with_supplier") if role == "customer" else m.get("with_customer")
    other = "поставщик" if role == "customer" else "заказчик"
    if top and linked:
        if linked < settings.ORG_MIN_CONTRACTS:
            facts.append(f"{other.capitalize()} указан в данных только у {linked} из {n} закупок — для выводов о концентрации и "
                         f"«любимом {other}е» данных мало (нужно не меньше {settings.ORG_MIN_CONTRACTS}).")
        elif len(top) > 1 and top[0]["contracts"] == top[1]["contracts"]:
            facts.append(f"Контрагентов: {len(top)}; у нескольких одинаковое число контрактов — ни один не преобладает.")
        else:
            t = top[0]
            facts.append(f"Крупнейший {other} — {t['name'] or t['inn']}: {t['contracts']} из {linked} контрактов ({_pct(t['share'])}).")
    if m.get("analyzed"):
        facts.append(f"Проанализировано закупок: {m['analyzed']}, средний балл {m['avg_score']}, в красной зоне {m['red_zone']}.")
    if section["risks"]:
        facts.append("Признаки риска: " + "; ".join(f"{r['rule_id']} — {r['description'] or r['title']}"
                                                   for r in section["risks"][:4]) + ".")
    else:
        facts.append("Признаков риска по правилам не выявлено.")
    zones = {"green": "зелёная зона", "yellow": "жёлтая зона", "red": "красная зона"}
    facts.append(f"Балл риска {section['score']} ({zones.get(section['zone'], section['zone'])}).")
    return facts


def default_resume(role: str, name: str, section: Dict[str, Any], settings: Any = None) -> str:
    """Резюме секции шаблоном (без модели): название и факты :func:`resume_facts`."""
    if settings is None:
        from configs import settings
    who = "Заказчик" if role == "customer" else "Поставщик"
    return f"{who} {name}. " + " ".join(resume_facts(role, section, settings))

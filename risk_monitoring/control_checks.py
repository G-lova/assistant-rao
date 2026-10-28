"""Внеплановые проверки контрольных органов (``checkResult`` без жалобы) в оценке закупок и заказчиков.

Одна проверка охватывает много закупок (у результатов ФАС России от 26–27.10.2026 — по 50 закупок десятков
заказчиков), а субъект проверки — не всегда заказчик: часто уполномоченный орган или специализированная
организация, проводившая закупки. Поэтому:

- закупка из списка проверки получает риск PROC-008 «Жалобы и контроль по процедуре»: сильный, если проверяли
  самого заказчика, и слабый, если — организацию, проводившую закупку;
- у заказчика нарушения считаются только по проверкам, где он сам субъект (BEH-001); проверки организаций,
  проводивших его закупки, — метрика без риска.

Сведения проверок — ``ai_analysis.facts`` XML (``xml_events.check_facts``).
"""
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from risk_monitoring import risk_catalog


def _list(v: Any) -> List[Any]:
    """Список из JSON-строки или списка (``None`` — пусто)."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return list(v) if isinstance(v, (list, tuple)) else []


def _flag(v: Any) -> bool:
    """Логическое значение из JSON (``true`` / ``1``)."""
    return str(v).strip().lower() in ("true", "1")


def normalize(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Проверки из строк ``checks_recent.sql`` или из ``facts`` событий прогона — без повторов по номеру.

    Args:
        rows: Строки запроса (поля ``check_*``) или ``facts`` XML (те же имена) с ``id``.

    Returns:
        List[Dict[str, Any]]: ``xml_id``, ``number``, ``result``, ``prescription``, ``date``, ``subjects``,
        ``purchases`` (множество), ``customer_inns`` (множество).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        num = r.get("check_number")
        if not num:
            continue
        out[str(num)] = {"xml_id": r.get("id") or r.get("xml_id"), "number": str(num),
                         "result": r.get("check_result"), "prescription": _flag(r.get("check_prescription")),
                         "date": str(r.get("check_decision_date") or r.get("event_at") or "")[:10] or None,
                         "subjects": [s for s in _list(r.get("check_subjects")) if isinstance(s, dict)],
                         "purchases": {str(p) for p in _list(r.get("check_purchases"))},
                         "customer_inns": {str(i) for i in _list(r.get("check_customer_inns"))}}
    return list(out.values())


def purchases_of(checks: Sequence[Dict[str, Any]]) -> List[str]:
    """Все номера закупок из проверок."""
    return sorted({p for c in checks for p in c["purchases"]})


def _subject_is(check: Dict[str, Any], inn: Optional[str]) -> bool:
    """Субъект проверки — организация с этим ИНН."""
    return bool(inn) and any(str(s.get("inn")) == str(inn) for s in check["subjects"])


def check_risks(checks: Sequence[Dict[str, Any]], purchase_number: Optional[str], customer_inn: Optional[str],
                cfg: Any) -> List[Dict[str, Any]]:
    """Риски закупки по проверкам с выявленными нарушениями, в список закупок которых она входит.

    Args:
        checks: :func:`normalize`.
        purchase_number: Номер закупки.
        customer_inn: ИНН заказчика закупки.
        cfg: Настройки (``CHECK_RISK_LEVELS``).

    Returns:
        List[Dict[str, Any]]: Риски PROC-008 во внешнем формате (``source = control``).
    """
    if not purchase_number:
        return []
    out = []
    for c in checks:
        if str(purchase_number) not in c["purchases"] or c["result"] != "violations":
            continue
        own = _subject_is(c, customer_inn)
        level = cfg.CHECK_RISK_LEVELS["customer" if own else "other"] + (cfg.CHECK_PRESCRIPTION_BONUS if c["prescription"] else 0)
        who = ", ".join(f"{s.get('name') or s.get('inn')}" for s in c["subjects"][:2]) or "субъект не указан"
        out.append({"rule_id": "PROC-008", "title": risk_catalog.title("PROC-008"),
                    "description": (f"Внеплановая проверка {c['number']} ({c['date'] or 'дата не указана'}) выявила "
                                    f"нарушения" + (", выдано предписание" if c["prescription"] else "")
                                    + ("; проверяли заказчика" if own else
                                       f"; проверяли организацию, проводившую закупку ({who}), закупка — одна из "
                                       f"{len(c['purchases'])} в проверке")),
                    "level": round(min(1.0, level), 2), "confidence": 1.0 if own else 0.7, "law": None,
                    "evidence": [{"src": f"xml:{c['xml_id']}", "check_number": c["number"], "check_result": c["result"],
                                  "prescription": c["prescription"], "subjects": c["subjects"][:3]}],
                    "verification_needed": ["Изучить решение и предписание контрольного органа по закупке"],
                    "source": "control"})
    return out


def customer_metrics(checks: Sequence[Dict[str, Any]], inn: str, purchases: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """Проверки заказчика: где он сам субъект и где проверяли организацию, проводившую его закупки.

    Args:
        checks: :func:`normalize`.
        inn: ИНН заказчика.
        purchases: Номера закупок заказчика.

    Returns:
        Optional[Dict[str, Any]]: ``None`` — проверок нет. Иначе ``as_subject``, ``as_subject_violations``,
        ``prescriptions``, ``via_purchases`` (проверки других субъектов по его закупкам), ``via_purchases_violations``,
        ``purchases_checked``, ``numbers``.
    """
    purchases = {str(p) for p in purchases if p}
    own = [c for c in checks if _subject_is(c, inn)]
    via = [c for c in checks if c not in own and (inn in c["customer_inns"] or c["purchases"] & purchases)]
    if not own and not via:
        return None
    return {"as_subject": len(own), "as_subject_violations": sum(1 for c in own if c["result"] == "violations"),
            "prescriptions": sum(1 for c in own if c["prescription"]),
            "via_purchases": len(via), "via_purchases_violations": sum(1 for c in via if c["result"] == "violations"),
            "purchases_checked": len({p for c in own + via for p in c["purchases"] & purchases}),
            "numbers": [c["number"] for c in own + via][:20]}


def customer_risks(m: Optional[Dict[str, Any]], cfg: Any) -> List[Dict[str, Any]]:
    """BEH-001 заказчика: внеплановые проверки, где он сам субъект, выявили нарушения."""
    if not m or m["as_subject_violations"] < cfg.CUSTOMER_CHECK_VIOLATIONS_MIN:
        return []
    return [{"rule_id": "BEH-001", "title": risk_catalog.title("BEH-001"),
             "description": f"Внеплановые проверки контрольных органов выявили нарушения у заказчика: "
                            f"{m['as_subject_violations']} из {m['as_subject']}"
                            + (f", предписаний — {m['prescriptions']}" if m["prescriptions"] else ""),
             "level": 0.6, "confidence": 1.0, "law": None,
             "evidence": [{"src": "aggregate", "checks": m["numbers"][:5]}],
             "verification_needed": ["Проверить исполнение предписаний и устранение нарушений"]}]

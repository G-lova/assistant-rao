"""Положения о закупке (223-ФЗ) организаций справочника за год — список для проверки (решение 11.11.2026).

Организации, которые тратят собственные (приносящие доход) средства, закупают по 223-ФЗ по положению о закупке:
типовому положению учредителя (ч. 2.1 ст. 2 223-ФЗ) или собственному. Приказа Минобрнауки с перечнем таких
организаций нет, поэтому собираем положения по всем организациям справочника, а организации без положения
передаются юристу (Василию): нарушение это или приносящей доход деятельности нет.

Статус организации (``status``) за год:

=====================  ============================================================================================
``typal``              есть в списке заказчиков типового положения, версия которого действовала в году
``own``                собственное положение (``orderClause``) действовало в году
``joined``             присоединение к положению другой организации (``orderClause`` с головной организацией)
``none_with_223``      положения нет, а закупки по 223-ФЗ в году есть — сильный сигнал
``none``               положения нет и закупок по 223-ФЗ нет — возможно, нет приносящей доход деятельности
=====================  ============================================================================================

Версия «действовала в году», если её период применения — от ``use_for_base_date`` до даты следующей версии того же
положения — пересекается с годом. Организация, выведенная из типового положения в течение года, тоже считается
покрытой (в части года); такие случаи помечаются (``note``).

Если у организации есть и собственное, и типовое положение, в ``note`` — «собственное при типовом учредителя»: для
бюджетного учреждения при типовом положении учредителя собственное может быть нарушением — проверить.

Полнота зависит от дозагрузки за год (``clause_backfill``): ``coverage`` отчёта — сколько пар «регион × день»
загружено.
"""
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

STATUS_NAMES = {
    "typal": "Типовое положение", "own": "Собственное положение", "joined": "Присоединение к положению",
    "none_with_223": "Нет положения, есть закупки по 223-ФЗ", "none": "Нет положения",
}

SQL_VERSIONS = """
SELECT c.id, c.registration_number, c.version, c.use_for_base_date, c.clause_type, c.head_registration_number,
       c.name, c.creator_name, c.creator_inn, c.data -> 'head' ->> 'name' AS head_name, cu.inn
FROM rm_typal_clause_customers cu JOIN rm_typal_clauses c ON c.id = cu.clause_id
WHERE cu.inn = ANY($1::text[])
"""

# даты всех версий тех же положений (для границы периода применения версии)
SQL_ALL_VERSIONS = """
SELECT registration_number, version, use_for_base_date FROM rm_typal_clauses
WHERE registration_number = ANY($1::text[])
"""


def _d(v: Any) -> Optional[date]:
    """Дата из ``date`` / строки."""
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def active_in_year(versions: Sequence[Dict[str, Any]], all_versions: Dict[str, List[Dict[str, Any]]],
                   year: int) -> List[Dict[str, Any]]:
    """Версии, период применения которых пересекается с годом.

    Args:
        versions: Версии с организацией (``registration_number``, ``version``, ``use_for_base_date``).
        all_versions: Все версии тех же положений по номеру (граница — дата следующей версии).
        year: Год.

    Returns:
        List[Dict[str, Any]]: Версии из ``versions`` с ``active_from`` / ``active_to``.
    """
    y0, y1 = date(year, 1, 1), date(year, 12, 31)
    out = []
    for v in versions:
        start = _d(v.get("use_for_base_date")) or date.min
        later = sorted(_d(x.get("use_for_base_date")) or date.min for x in all_versions.get(v["registration_number"], [])
                       if int(x["version"]) > int(v["version"]))
        nxt = next((d for d in later if d > start), None)
        end = (nxt - timedelta(days=1)) if nxt else date.max
        if start <= y1 and end >= y0:
            out.append({**v, "active_from": None if start == date.min else start.isoformat(),
                        "active_to": None if end == date.max else end.isoformat()})
    return out


def classify(org: Dict[str, Any], active: Sequence[Dict[str, Any]], purchases_223: int, year: int) -> Dict[str, Any]:
    """Статус организации за год по действовавшим версиям положений и закупкам по 223-ФЗ.

    Returns:
        Dict[str, Any]: строка отчёта (организация, ``status``, ``status_name``, реквизиты положения, ``note``).
    """
    typal = [v for v in active if v["clause_type"] == "typal"]
    own = [v for v in active if v["clause_type"] == "customer" and not v.get("head_registration_number")]
    joined = [v for v in active if v["clause_type"] == "customer" and v.get("head_registration_number")]
    notes = []
    if own:
        status, main = "own", own
    elif joined:
        status, main = "joined", joined
    elif typal:
        status, main = "typal", typal
    else:
        status, main = ("none_with_223" if purchases_223 else "none"), []
    if own and typal:
        notes.append("собственное положение при типовом учредителя — проверить (ч. 2.1 ст. 2 223-ФЗ)")
    if typal and all(v.get("active_to") and v["active_to"] < f"{year}-12-31" for v in typal) and not (own or joined):
        notes.append("выведена из типового положения в течение года")
    last = max(main, key=lambda v: (str(v.get("use_for_base_date") or ""), int(v["version"]))) if main else None
    return {
        "org_id": org.get("id"), "inn": org.get("inn"), "kpp": org.get("kpp"), "name": org.get("name"),
        "region": org.get("region"), "unit_type": org.get("unit_type"), "okogu": org.get("okogu_name"),
        "monitoring": org.get("monitoring"), "status": status, "status_name": STATUS_NAMES[status],
        "clause_number": last["registration_number"] if last else None,
        "clause_version": last["version"] if last else None,
        "clause_from": last.get("active_from") if last else None,
        "clause_name": last.get("name") if last else None,
        "clause_owner": ((last.get("creator_name") if last["clause_type"] == "typal" else last.get("head_name"))
                         if last else None),
        "purchases_223": purchases_223, "note": "; ".join(notes) or None,
    }


async def build(pool: Any, orgs: Sequence[Dict[str, Any]], purchases: Dict[Any, int], year: int) -> Dict[str, Any]:
    """Отчёт по организациям.

    Args:
        pool: Пул ``asyncpg`` сервиса.
        orgs: Головные организации справочника (``clause_orgs.sql``).
        purchases: Число закупок по 223-ФЗ за год по ``org_id``.
        year: Год.

    Returns:
        Dict[str, Any]: ``rows`` (по ИНН, без повторов), ``totals`` по статусам, ``coverage`` журнала дозагрузки.
    """
    by_inn: Dict[str, Dict[str, Any]] = {}
    ids_by_inn: Dict[str, List[Any]] = defaultdict(list)
    for o in orgs:
        inn = str(o.get("inn") or "").strip()
        if not inn:
            continue
        ids_by_inn[inn].append(o.get("id"))
        # одна строка на ИНН: актуальная запись справочника, на мониторинге — важнее
        cur = by_inn.get(inn)
        key = (str(o.get("is_actual")) == "1", str(o.get("monitoring")) == "1")
        if cur is None or key > (str(cur.get("is_actual")) == "1", str(cur.get("monitoring")) == "1"):
            by_inn[inn] = o
    rows = [dict(r) for r in await pool.fetch(SQL_VERSIONS, sorted(by_inn))]
    regs = sorted({r["registration_number"] for r in rows})
    allv: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in await pool.fetch(SQL_ALL_VERSIONS, regs):
        allv[r["registration_number"]].append(dict(r))
    versions_by_inn: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        versions_by_inn[r["inn"]].append(r)
    out = []
    for inn, org in sorted(by_inn.items(), key=lambda kv: str(kv[1].get("name") or "")):
        active = active_in_year(versions_by_inn.get(inn, []), allv, year)
        n223 = sum(int(purchases.get(i) or 0) for i in ids_by_inn[inn])
        out.append(classify(org, active, n223, year))
    totals: Dict[str, int] = defaultdict(int)
    for r in out:
        totals[r["status"]] += 1
    try:
        cov = [dict(r) for r in await pool.fetch(
            "SELECT kind, COUNT(*) FILTER (WHERE status IN ('done', 'no_data')) AS loaded, "
            "COUNT(*) FILTER (WHERE status = 'error') AS errors, MIN(day) AS first_day, MAX(day) AS last_day "
            "FROM rm_eis_backfill_log WHERE day BETWEEN $1 AND $2 GROUP BY kind", date(year, 1, 1), date(year, 12, 31))]
    except Exception:  # noqa: BLE001 — до миграции 016 журнала нет
        cov = []
    return {"year": year, "rows": out, "totals": dict(totals),
            "coverage": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in c.items()} for c in cov]}


COLUMNS = [("name", "Организация", 60), ("inn", "ИНН", 13), ("kpp", "КПП", 11), ("region", "Регион", 22),
           ("unit_type", "Вид", 12), ("okogu", "Ведомство (ОКОГУ)", 30), ("status_name", "Положение в году", 30),
           ("clause_number", "№ положения в ЕИС", 16), ("clause_version", "Редакция", 9),
           ("clause_from", "Применяется с", 13), ("clause_owner", "Чьё положение", 40),
           ("purchases_223", "Закупок по 223-ФЗ за год", 12), ("note", "Пометка", 45),
           ("decision", "Решение (заполняет юрист)", 30), ("comment", "Комментарий", 30)]


def to_xlsx(report: Dict[str, Any], path: str) -> str:
    """Таблица для юриста: все организации (сначала без положения), итоги и полнота загрузки на втором листе."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    order = {"none_with_223": 0, "none": 1, "own": 2, "joined": 3, "typal": 4}
    rows = sorted(report["rows"], key=lambda r: (order.get(r["status"], 9), str(r.get("name") or "")))
    wb = Workbook()
    ws = wb.active
    ws.title = f"Положения {report['year']}"
    ws.append([c[1] for c in COLUMNS])
    for i, (_, _, w) in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        ws.cell(1, i).font = Font(bold=True)
        ws.cell(1, i).alignment = Alignment(wrap_text=True, vertical="top")
    fill = {"none_with_223": "F8D7DA", "none": "FFF3CD", "own": "E2E3E5", "joined": "E2E3E5"}
    for r in rows:
        ws.append([r.get(k) for k, _, _ in COLUMNS])
        color = fill.get(r["status"])
        if color:
            for i in range(1, len(COLUMNS) + 1):
                ws.cell(ws.max_row, i).fill = PatternFill("solid", fgColor=color)
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    s = wb.create_sheet("Итоги")
    s.append(["Положение", "Организаций"])
    for k, name in STATUS_NAMES.items():
        s.append([name, report["totals"].get(k, 0)])
    s.append([])
    s.append(["Полнота загрузки ЕИС за год", "загружено пар «регион × день»", "ошибок", "с", "по"])
    for c in report.get("coverage") or []:
        s.append([{"typal": "типовые положения", "customer": "положения заказчиков"}.get(c["kind"], c["kind"]),
                  c.get("loaded"), c.get("errors"), c.get("first_day"), c.get("last_day")])
    s.column_dimensions["A"].width = 40
    s.column_dimensions["B"].width = 30
    wb.save(path)
    return path


def purchases_by_org(df: Any) -> Dict[Any, int]:
    """``orgs_purchases_223.sql`` → число закупок по ``org_id``."""
    out: Dict[Any, int] = {}
    ids = list(df.get("org_id", [])) if hasattr(df, "get") else []
    nums = list(df.get("purchases", [])) if hasattr(df, "get") else []
    for i, n in zip(ids, nums):
        try:
            out[int(i)] = int(n or 0)
        except (TypeError, ValueError):
            continue
    return out


def records(df: Any) -> List[Dict[str, Any]]:
    """DataFrame / dict списков → список строк."""
    if hasattr(df, "to_dict"):
        try:
            return df.to_dict("records")
        except TypeError:
            pass
    if isinstance(df, dict):
        keys = list(df)
        return [dict(zip(keys, vals)) for vals in zip(*(df[k] for k in keys))]
    return list(df or [])

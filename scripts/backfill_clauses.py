"""Дозагрузка положений о закупке (223-ФЗ) из ЕИС за период и отчёт о положениях организаций.

Долгая (часы) и возобновляемая: выполненные пары «вид × регион × день» записаны в ``rm_eis_backfill_log``
(миграция 016) и при повторном запуске пропускаются. Запускать в фоне в контейнере сервиса::

    docker exec -d rao_api sh -c "python -m scripts.backfill_clauses --env prod > /tmp/backfill.log 2>&1"
    docker exec rao_api tail -f /tmp/backfill.log                       # ход
    docker exec rao_api python -m scripts.backfill_clauses --env prod --status   # сводка журнала

Порядок: типовые положения (регион 77 первым), затем положения заказчиков; дни — от последнего к первому. После
загрузки читаются файлы только последних версий положений (``--no-files`` — пропустить). Отчёт для юриста —
``python -m scripts.backfill_clauses --env prod --report /tmp/polozheniya_2026.xlsx`` (без загрузки).
"""
import argparse
import asyncio
import json
from datetime import date


async def org_rows(env: str, http_manager):
    """Головные организации справочника окружения (``clause_orgs.sql``)."""
    from configs.config import Config
    from configs.data_fetcher import DataFetcher
    from risk_monitoring import clause_coverage, rm_sql

    db = Config().get_database_config(env)
    sql, b = rm_sql.render("clause_orgs.sql")
    df = await DataFetcher(db.url, db.headers, http_manager).fetch_async_expertise_data(sql, bindings=b)
    return clause_coverage.records(df)


async def purchases_223(env: str, http_manager, year: int):
    """Закупки по 223-ФЗ организаций за год (``orgs_purchases_223.sql``)."""
    from configs.config import Config
    from configs.data_fetcher import DataFetcher
    from risk_monitoring import clause_coverage, rm_sql

    db = Config().get_database_config(env)
    sql, b = rm_sql.render("orgs_purchases_223.sql", head=[f"{year}-01-01", f"{year + 1}-01-01"])
    df = await DataFetcher(db.url, db.headers, http_manager).fetch_async_expertise_data(sql, bindings=b)
    return clause_coverage.purchases_by_org(df)


async def main(a):
    """Дозагрузка, чтение файлов последних версий, сводка или отчёт — по аргументам."""
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager
    from configs.logger import get_logger
    from risk_monitoring import clause_coverage
    from risk_monitoring.clause_backfill import ClauseBackfill
    from risk_monitoring.typal_loader import TypalLoader

    log = get_logger("backfill_clauses")
    if a.interval:
        from configs import settings

        settings.TYPAL_EIS_MIN_INTERVAL_SEC = max(0.7, a.interval)
    async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
        if a.report:
            orgs = await org_rows(a.env, mgr)
            rep = await clause_coverage.build(repo.pool, orgs, await purchases_223(a.env, mgr, a.year), a.year)
            clause_coverage.to_xlsx(rep, a.report)
            print(json.dumps({"file": a.report, "organisations": len(rep["rows"]), "totals": rep["totals"],
                              "coverage": rep["coverage"]}, ensure_ascii=False, default=str))
            return
        loader = TypalLoader(mgr, repo, environment=a.env)
        orgs = await org_rows(a.env, mgr)
        bf = ClauseBackfill(loader, [o.get("inn") for o in orgs])
        if a.status:
            print(json.dumps(await bf.progress(), ensure_ascii=False, default=str, indent=1))
            return
        log.info(f"Дозагрузка положений: организаций {len(bf.inns)}, период {a.date_from} — {a.date_to}, "
                 f"виды {a.kinds}")
        res = await bf.run(date.fromisoformat(a.date_from), date.fromisoformat(a.date_to), kinds=a.kinds.split(","),
                           regions=a.regions.split(",") if a.regions else None, max_requests=a.max_requests)
        print(json.dumps({k: v for k, v in res.items() if k != "regions"}, ensure_ascii=False, default=str))
        if not a.no_files and not res["left"]:
            loader.read_files = True
            print(json.dumps({"read_latest_files": await loader.read_latest_files(bf.inns)}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", required=True, help="окружение внешней системы (как X-API-Database): prod / stage / dev")
    ap.add_argument("--from", dest="date_from", default=f"{date.today().year}-01-01")
    ap.add_argument("--to", dest="date_to", default=date.today().isoformat())
    ap.add_argument("--kinds", default="typal,customer", help="typal,customer")
    ap.add_argument("--regions", help="коды регионов через запятую (по умолчанию — по ИНН организаций)")
    ap.add_argument("--max-requests", type=int, help="остановиться после стольких запросов к ЕИС")
    ap.add_argument("--interval", type=float,
                    help="секунд между запросами к ЕИС (по умолчанию TYPAL_EIS_MIN_INTERVAL_SEC = 1.5; лимит ЕИС — "
                         "90 запросов в минуту на адрес вместе с внешней системой)")
    ap.add_argument("--no-files", action="store_true", help="не читать файлы последних версий после загрузки")
    ap.add_argument("--status", action="store_true", help="только сводка журнала дозагрузки")
    ap.add_argument("--report", help="только отчёт: путь к .xlsx")
    ap.add_argument("--year", type=int, default=date.today().year, help="год отчёта")
    asyncio.run(main(ap.parse_args()))

"""Разовый пересчёт всех закупок окружения по готовым результатам документов (без повторного анализа файлов и XML).

Нужен, когда меняется состав паспорта или рисков закупки (стадии и состояния 11.11.2026, находки 12.11.2026):
ежедневный прогон пересчитывает только закупки с событиями дня. Закупки ставятся в очередь Celery
``rm_analyze_contracts_task`` пачками (``analyze_documents = False``); у закупки с прежними входными данными
(``input_hash``) пересчёт не выполняется, поэтому повторный запуск дешёвый. Резюме закупки пишет модель (один запрос
на закупку); ``--no-llm`` — шаблонное резюме, быстрее.

Запуск::

    docker exec rao_api python -m scripts.recompute_contracts --env prod
    docker exec rao_api python -m scripts.recompute_contracts --env prod --batch 100 --no-llm
"""
import argparse
import asyncio
import json


async def ids(env: str):
    """Id закупок с баллом (``contracts_scored_ids.sql``)."""
    from configs.config import Config
    from configs.data_fetcher import DataFetcher
    from configs.http_client_manager import HTTPClientManager
    from risk_monitoring import rm_sql

    db = Config().get_database_config(env)
    sql, b = rm_sql.render("contracts_scored_ids.sql")
    async with HTTPClientManager(timeout=120.0) as mgr:
        df = await DataFetcher(db.url, db.headers, mgr).fetch_async_expertise_data(sql, bindings=b)
    return [int(x) for x in df.get("id", [])]


def main(a):
    """Ставит пачки в очередь и печатает id задач."""
    from risk_monitoring.tasks import rm_analyze_contracts_task

    all_ids = asyncio.run(ids(a.env))
    tasks = []
    for i in range(0, len(all_ids), a.batch):
        part = all_ids[i:i + a.batch]
        t = rm_analyze_contracts_task.apply_async(
            args=[part, a.env], kwargs={"force": False, "push": True, "use_llm": not a.no_llm,
                                       "include_analysis": False, "analyze_documents": False},
            queue="risk_monitoring")
        tasks.append(t.id)
    print(json.dumps({"contracts": len(all_ids), "batches": len(tasks), "task_ids": tasks}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", required=True, help="окружение внешней системы: prod / stage / dev")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--no-llm", action="store_true", help="резюме закупки по шаблону, без модели")
    main(ap.parse_args())

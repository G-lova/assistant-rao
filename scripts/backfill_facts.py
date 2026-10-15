"""Backfill фактов по уже сохранённым текстам (шаг 4.3): без повторного чтения файлов и OCR.

Для каждой экспертизы определяет форму заключения по паспорту закупки, строит факты раздела 1 из
XML извещения и (если не задан ``--xml-only``) факты по остальным критериям через RAG + LLM.

Запуск (внутри контейнера приложения)::

    python -m scripts.backfill_facts --env prod 5885 5887   # конкретные экспертизы окружения prod
    python -m scripts.backfill_facts --env dev --all --xml-only   # все экспертизы dev с текстами, без LLM
"""
import argparse
import asyncio
import sys

from knowledge_store import facts, forms, repository as repo
from knowledge_store.scope import Scope


async def process(conn, scope: Scope, extractor) -> str:
    """Строит факты одной экспертизы.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        extractor: :class:`knowledge_store.facts.FactExtractor` или ``None`` (только XML).

    Returns:
        str: Строка отчёта по экспертизе.
    """
    passport = await repo.get_procurement(conn, scope)
    if not passport:
        return f"{scope}: нет паспорта закупки (экспертиза не проходила через /evaluate-documents)"
    code = forms.get_form_code(passport["law"], passport["check_type2"], passport["object_code"],
                               available=await repo.list_form_codes(conn))
    await repo.set_form_code(conn, scope, code)
    if not code:
        return (f"{scope}: форма не поддерживается "
                f"({passport['law']}, checkType2={passport['check_type2']}, объект {passport['object_code']})")
    stats = await facts.extract_facts(conn, scope, code, extractor)
    return f"{scope}: {code} → {stats}"


async def main(argv=None) -> int:
    """Точка входа: разбирает аргументы, создаёт клиентов и обрабатывает экспертизы по очереди.

    Args:
        argv: Аргументы командной строки (для тестов).

    Returns:
        int: Код возврата процесса.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", type=int, help="ID экспертиз")
    parser.add_argument("--env", required=True, help="окружение (X-API-Database): dev / stage / prod")
    parser.add_argument("--all", action="store_true", help="все экспертизы с сохранёнными текстами")
    parser.add_argument("--xml-only", action="store_true", help="только факты из XML, без LLM и эмбеддингов")
    args = parser.parse_args(argv)
    if not args.ids and not args.all:
        parser.error("укажите ID экспертиз или --all")

    from configs.working_with_db import get_async_db_connection
    from configs.http_client_manager import HTTPClientManager

    async with HTTPClientManager(timeout=120.0) as mgr:
        extractor = None
        if not args.xml_only:
            from configs.llm_client import get_llm
            from knowledge_store.indexer import build_embedder
            client, model = get_llm()
            extractor = facts.FactExtractor(facts.make_llm_call(client, model), build_embedder(mgr))
        async with get_async_db_connection() as conn:
            env = Scope.of(args.env, 0).env
            scopes = ([Scope(env, int(i)) for i in args.ids] if args.ids
                      else await repo.list_expertises_with_texts(conn, env))
            for scope in scopes:
                async with conn.transaction():
                    print(await process(conn, scope, extractor))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

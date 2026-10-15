"""Переиндексация экспертиз в ``ai_document_chunks`` (нарезка ``PE_CHUNKER`` или ``--chunker``) без прогона и фактов.

Нужна после миграции 005: старые экспертизы проиндексированы в ``pe_chunks``, а поиск фактов теперь работает по
``ai_document_chunks`` с нарезкой ``PE_CHUNKER`` (по умолчанию прежняя ``pages_1500``). Скрипт **не** вызывает ``/evaluate-documents`` (тот удалил бы ссылки экспертизы и
старые ``pe_chunks``, нужные для сравнения нарезок) и **не** строит факты (факты, построенные по старой нарезке,
остаются эталоном для ``scripts.eval_chunkers recall``). Документ, уже проиндексированный для другой экспертизы,
повторно не индексируется.

Запуск (внутри контейнера приложения, параметры БД — ``DB_*``, модель эмбеддингов — ``EMBEDDING_*``)::

    python -m scripts.reindex_pages_v2 --env prod --all                       # нарезка PE_CHUNKER (pages_1500)
    python -m scripts.reindex_pages_v2 --env dev --ids 6189,6190 --chunker pages_v2
    python -m scripts.reindex_pages_v2 --env dev --all --with-facts   # и сразу перестроить факты (вызовы LLM)
"""
import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List

from knowledge_store.scope import Scope, normalize_env


async def reindex(scopes: List[Scope], http_manager: Any, with_facts: bool = False) -> Dict[str, Any]:
    """Индексирует экспертизы по очереди; сбой одной не останавливает остальные.

    Args:
        scopes: Экспертизы.
        http_manager: Общий ``HTTPClientManager``.
        with_facts: Построить факты после индексации (как ``index_documents_task``).

    Returns:
        dict: По экспертизе — статистика индексации (и фактов) или ошибка; ``total`` — сумма.
    """
    from knowledge_store import indexer, runner

    embedder = indexer.build_embedder(http_manager)
    client = model = None
    if with_facts:
        from configs.llm_client import get_llm
        client, model = get_llm()
    report: Dict[str, Any] = {}
    total = {"documents": 0, "chunks": 0, "failed": 0}
    for scope in scopes:
        try:
            stats = await indexer.index_expertise(scope, embedder)
            for key in total:
                total[key] += stats.get(key, 0)
            entry: Dict[str, Any] = {"index": stats}
            if with_facts:
                entry["facts"] = await runner.build_facts(scope, embedder, client, model)
            report[str(scope)] = entry
        except Exception as e:  # noqa: BLE001
            report[str(scope)] = {"error": f"{type(e).__name__}: {e}"[:300]}
        print(f"{scope}: {report[str(scope)]}", file=sys.stderr)
    report["total"] = total
    return report


async def main(argv=None) -> int:
    """Точка входа.

    Args:
        argv: Аргументы командной строки.

    Returns:
        int: Код возврата.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="окружение (X-API-Database)")
    parser.add_argument("--ids", help="ID экспертиз через запятую")
    parser.add_argument("--all", action="store_true", help="все экспертизы окружения с сохранёнными текстами")
    parser.add_argument("--with-facts", action="store_true", help="после индексации перестроить факты (LLM)")
    parser.add_argument("--chunker", help="нарезка вместо PE_CHUNKER: pages_1500 | pages_v2")
    args = parser.parse_args(argv)
    if args.chunker:
        import os

        from knowledge_store.pages import CHUNKERS
        if args.chunker not in CHUNKERS:
            parser.error(f"неизвестная нарезка {args.chunker}; допустимо: {', '.join(CHUNKERS)}")
        os.environ["PE_CHUNKER"] = args.chunker
    if not args.ids and not args.all:
        parser.error("укажите --ids или --all")

    from configs.http_client_manager import HTTPClientManager
    from knowledge_store import db, repository as repo

    env = normalize_env(args.env)
    async with HTTPClientManager(timeout=120.0) as mgr, db.task_pool():
        if args.ids:
            scopes = [Scope(env, int(x)) for x in args.ids.split(",") if x.strip()]
        else:
            async with db.connection() as conn:
                scopes = await repo.list_expertises_with_texts(conn, env)
        result = await reindex(scopes, mgr, args.with_facts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

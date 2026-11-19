"""Выгрузка данных хранилища знаний для проверки текстовых ответов первичных экспертов по фактам.

Только чтение. Для экспертиз окружения (``--env``) пишет в каталог ``--out`` четыре CSV:

* ``pe_documents.csv`` — документы без полного текста (``text_full`` не выгружается): имя файла, код и тип, число страниц,
  ``extraction`` (реквизиты, извлечённые из документа);
* ``pe_procurements.csv`` — паспорт закупки (закон, способ, форма, НМЦК, аванс, прочее);
* ``pe_facts.csv`` — факты с цитатами (источник, уверенность);
* ``pe_summary_opinions.csv`` — последнее сформированное сводное заключение экспертизы (данные и трассировка).

Запуск (в контейнере dev)::

    python -m scripts.export_primary_facts --env dev --out /tmp/primary_facts
    python -m scripts.export_primary_facts --env dev --out /tmp/primary_facts --ids 7283 7365 5264

Без ``--ids`` берутся все экспертизы окружения, у которых есть документы. Сначала проверьте объём: ``--count``.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

from knowledge_store.scope import Scope

SQL_EXPERTISES = "SELECT DISTINCT expertise_id FROM pe_documents WHERE env = $1 ORDER BY expertise_id"
SQL_DOCUMENTS = """
SELECT expertise_id, id, doc_code, detected_type, filename, pages_count, (text_full IS NOT NULL) AS has_text, extraction
FROM pe_documents WHERE env = $1 AND expertise_id = ANY($2::int[]) ORDER BY expertise_id, id
"""
SQL_PROCUREMENTS = """
SELECT expertise_id, law, method, object_code, check_type2, form_code, nmck, advance, funding, passport
FROM pe_procurements WHERE env = $1 AND expertise_id = ANY($2::int[]) ORDER BY expertise_id
"""
SQL_FACTS = """
SELECT expertise_id, fact_key, value, document_id, page, quote, confidence, source
FROM pe_facts WHERE env = $1 AND expertise_id = ANY($2::int[]) ORDER BY expertise_id, id
"""
SQL_SUMMARIES = """
SELECT DISTINCT ON (expertise_id) expertise_id, form_code, status, created_at, data, trace
FROM pe_summary_opinions WHERE env = $1 AND expertise_id = ANY($2::int[]) ORDER BY expertise_id, created_at DESC
"""


def cell(value: Any) -> str:
    """Значение ячейки CSV: JSON-поля (``dict``/``list``) — строкой JSON, остальное — как есть, ``None`` — пусто."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and value[:1] in "{[":          # asyncpg отдаёт JSONB строкой — оставляем как есть
        return value
    return str(value)


def write_csv(path: str, rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> int:
    """Пишет строки в CSV (UTF-8, с заголовком) и возвращает их число."""
    csv.field_size_limit(sys.maxsize)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell(row.get(c)) for c in columns])
    return len(rows)


async def export(conn, env: str, out_dir: str, ids: Optional[Iterable[int]] = None) -> Dict[str, int]:
    """Выгружает четыре таблицы для экспертиз окружения.

    Args:
        conn: Соединение ``asyncpg``.
        env: Окружение (``Scope.of(env, 0).env``).
        out_dir: Каталог результата (создаётся).
        ids: Экспертизы; ``None`` — все, у которых есть документы.

    Returns:
        dict: Число строк по файлам.
    """
    os.makedirs(out_dir, exist_ok=True)
    expertise_ids = list(ids) if ids else [r["expertise_id"] for r in await conn.fetch(SQL_EXPERTISES, env)]
    counts = {}
    for name, sql in (("pe_documents", SQL_DOCUMENTS), ("pe_procurements", SQL_PROCUREMENTS), ("pe_facts", SQL_FACTS),
                      ("pe_summary_opinions", SQL_SUMMARIES)):
        rows = [dict(r) for r in await conn.fetch(sql, env, expertise_ids)]
        columns = list(rows[0]) if rows else ["expertise_id"]
        counts[name] = write_csv(os.path.join(out_dir, f"{name}.csv"), rows, columns)
    counts["expertises"] = len(expertise_ids)
    return counts


async def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="окружение (X-API-Database): dev / stage")
    parser.add_argument("--out", default="primary_facts", help="каталог для CSV")
    parser.add_argument("--ids", nargs="*", type=int, help="ID экспертиз (по умолчанию — все с документами)")
    parser.add_argument("--count", action="store_true", help="только показать, сколько экспертиз с документами, и выйти")
    args = parser.parse_args(argv)

    from configs.working_with_db import get_async_db_connection

    async with get_async_db_connection() as conn:
        env = Scope.of(args.env, 0).env
        if args.count:
            print(f"экспертиз с документами в окружении {env}: {len(await conn.fetch(SQL_EXPERTISES, env))}")
            return 0
        print(json.dumps(await export(conn, env, args.out, args.ids), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

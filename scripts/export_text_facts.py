"""Выгрузка реквизитов из текстов документов экспертиз: даты, суммы, номера (без самих текстов).

Для проверки ответов первичных экспертов нужно знать, какие даты, суммы и номера встречаются в документах экспертизы.
Расширенные поля ``extraction`` захватывают лишь часть таких значений (на истории 72% дат первичных находятся в них, остальные
есть в самих текстах), поэтому реквизиты берутся из текстов. Скрипт читает тексты документов (только чтение; текст, очищенный
политикой хранения, пропускается), находит значения регулярными выражениями
(:func:`knowledge_store.primary_remarks.find_dates` / ``find_amounts`` / ``find_numbers``) и пишет только их.

Запуск (в контейнере dev)::

    python -m scripts.export_text_facts --env dev --ids-file ids.txt --out /tmp/document_facts.csv
    python -m scripts.export_text_facts --env dev --ids 6504 5858 --out /tmp/document_facts.csv

Результат — один CSV: ``expertise_id, document_id, filename, doc_code, has_text, dates, amounts, numbers`` (списки в JSON).
"""
import argparse
import asyncio
import csv
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from knowledge_store import primary_remarks, repository as repo
from knowledge_store.scope import Scope

COLUMNS = ["expertise_id", "document_id", "filename", "doc_code", "has_text", "dates", "amounts", "numbers"]
MAX_VALUES = 300          # значений каждого вида на документ (в сканах и таблицах чисел бывает очень много)


def document_row(expertise_id: int, document: Dict[str, Any]) -> Dict[str, Any]:
    """Строка выгрузки по одному документу: реквизиты из его текста (если текст есть)."""
    text = document.get("text_full") or ""
    return {
        "expertise_id": expertise_id, "document_id": document.get("id"), "filename": document.get("filename"),
        "doc_code": document.get("doc_code"), "has_text": bool(text),
        "dates": json.dumps(sorted(primary_remarks.find_dates(text))[:MAX_VALUES], ensure_ascii=False),
        "amounts": json.dumps(sorted(primary_remarks.find_amounts(text))[:MAX_VALUES]),
        "numbers": json.dumps(sorted(primary_remarks.find_numbers(text))[:MAX_VALUES], ensure_ascii=False),
    }


def read_ids(path: str) -> List[int]:
    """ID экспертиз из текстового файла (числа через пробелы, запятые или строки)."""
    with open(path, encoding="utf-8") as fh:
        return [int(x) for x in fh.read().replace(",", " ").split()]


async def export(conn, env: str, ids: Sequence[int], out_path: str) -> Dict[str, int]:
    """Пишет реквизиты документов экспертиз ``ids`` в CSV; возвращает число документов и документов с текстом."""
    docs = with_text = 0
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, COLUMNS)
        writer.writeheader()
        for expertise_id in ids:
            for document in await repo.get_documents_brief(conn, Scope(env, int(expertise_id))):
                row = document_row(int(expertise_id), dict(document))
                writer.writerow(row)
                docs += 1
                with_text += bool(row["has_text"])
    return {"expertises": len(ids), "documents": docs, "with_text": with_text}


async def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="окружение (X-API-Database): dev / stage")
    parser.add_argument("--ids", nargs="*", type=int, default=[], help="ID экспертиз")
    parser.add_argument("--ids-file", help="файл со списком ID экспертиз")
    parser.add_argument("--out", default="document_facts.csv")
    args = parser.parse_args(argv)
    ids = list(args.ids) + (read_ids(args.ids_file) if args.ids_file else [])
    if not ids:
        parser.error("укажите --ids или --ids-file")

    from configs.working_with_db import get_async_db_connection

    async with get_async_db_connection() as conn:
        print(json.dumps(await export(conn, Scope.of(args.env, 0).env, ids, args.out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

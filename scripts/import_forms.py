"""Импорт справочника форм заключения из «Отчеты и закупки поля.xlsx» в ``pe_form_fields``.

Запуск::

    python -m scripts.import_forms "Отчеты и закупки поля.xlsx"                 # запись в БД
    python -m scripts.import_forms "Отчеты и закупки поля.xlsx" --dry-run       # только сводка
    python -m scripts.import_forms "Отчеты и закупки поля.xlsx" --check-csv expertise_expert_opinion7s.csv

``--check-csv`` — шаг 3.2 плана: ключи ``data`` в выгрузке ``expertise_expert_opinion7s`` должны
входить в справочник своей формы. Форма записи определяется по характерным ключам из самого
справочника (без хардкода номеров полей).

Параметры БД берутся из переменных ``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER``, ``DB_PASSWORD``.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List

from knowledge_store import forms as ks_forms
from knowledge_store import repository as repo

SHEET_44FZ = "Все Закупки 44-фз"  # MVP: остальные листы (223-ФЗ, объект 7) — отдельным этапом


def load_forms(xlsx_path: str) -> Dict[str, List[ks_forms.FormField]]:
    """Читает Excel и возвращает справочник полей по формам.

    Args:
        xlsx_path: Путь к «Отчеты и закупки поля.xlsx».

    Returns:
        dict: ``код формы → список FormField``.
    """
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # openpyxl ругается на комментарии в объединённых ячейках
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    return ks_forms.parse_forms_sheet(wb[SHEET_44FZ])


def summarize(registry: Dict[str, List[ks_forms.FormField]]) -> str:
    """Формирует текстовую сводку справочника.

    Args:
        registry: Результат :func:`load_forms`.

    Returns:
        str: По строке на форму: число полей (из Excel / производных) и разбивка по типам.
    """
    lines = []
    for code, fields in registry.items():
        kinds = Counter(f.value_kind for f in fields)
        own = sum(1 for f in fields if not f.derived)
        lines.append(f"{code}: {len(fields)} полей (из Excel {own}, производных {len(fields) - own}); "
                     + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    return "\n".join(lines)


def guess_form(data: dict, registry: Dict[str, List[ks_forms.FormField]]) -> str:
    """Определяет форму записи заключения по пересечению ключей со справочником.

    Args:
        data: Словарь ``expertise_expert_opinion7s.data``.
        registry: Справочник полей.

    Returns:
        str: Код формы с наибольшей долей совпавших ключей или ``''``, если совпадений нет.
    """
    keys = set(data)
    best, best_score = "", 0.0
    for code, fields in registry.items():
        form_keys = {f.field_key for f in fields}
        score = len(keys & form_keys) / max(len(keys), 1)
        if score > best_score:
            best, best_score = code, score
    return best if best_score >= 0.5 else ""


def check_csv(csv_path: str, registry: Dict[str, List[ks_forms.FormField]]) -> str:
    """Шаг 3.2: проверяет, что ключи ``data`` входят в справочник формы.

    Args:
        csv_path: Выгрузка таблицы ``expertise_expert_opinion7s``.
        registry: Справочник полей.

    Returns:
        str: Отчёт: по каждой форме — число записей и ключи данных, которых нет в справочнике.
    """
    csv.field_size_limit(sys.maxsize)
    total, unknown_form = Counter(), 0
    extra = defaultdict(Counter)
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = row.get("data")
            if not raw or raw == "NULL":
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            code = guess_form(data, registry)
            if not code:
                unknown_form += 1
                continue
            total[code] += 1
            known = {f.field_key for f in registry[code]}
            for key in data:
                if key not in known and not key.startswith("rao_comment"):
                    extra[code][key] += 1
    lines = [f"записей без формы в справочнике (старые формы, 223-ФЗ, объект 7): {unknown_form}"]
    for code, n in total.most_common():
        miss = extra[code]
        lines.append(f"{code}: записей {n}, ключей вне справочника: {len(miss)}"
                     + (" → " + ", ".join(f"{k}({c})" for k, c in miss.most_common(15)) if miss else ""))
    return "\n".join(lines)


async def save(registry: Dict[str, List[ks_forms.FormField]]) -> None:
    """Записывает справочник в ``pe_form_fields`` (каждая форма — в своей транзакции).

    Args:
        registry: Результат :func:`load_forms`.
    """
    import asyncpg  # локальный импорт: остальные функции модуля работают без asyncpg

    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST"), port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME"), user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
    )
    try:
        for code, fields in registry.items():
            async with conn.transaction():
                n = await repo.replace_form_fields(conn, code, fields)
            print(f"записано {code}: {n} полей")
    finally:
        await conn.close()


def main(argv=None) -> int:
    """Точка входа командной строки.

    Args:
        argv: Аргументы (для тестов); по умолчанию ``sys.argv``.

    Returns:
        int: Код возврата процесса.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("xlsx", help="путь к «Отчеты и закупки поля.xlsx»")
    parser.add_argument("--dry-run", action="store_true", help="только сводка, без записи в БД")
    parser.add_argument("--check-csv", metavar="CSV", help="проверить ключи data из выгрузки expertise_expert_opinion7s")
    args = parser.parse_args(argv)

    registry = load_forms(args.xlsx)
    print(summarize(registry))
    if args.check_csv:
        print(check_csv(args.check_csv, registry))
    if not args.dry_run and not args.check_csv:
        asyncio.run(save(registry))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Строит ``knowledge_store/report_fields.json`` — реальные ключи заключений объекта 7 по выгрузке экспертов.

Excel описывает форму объекта 7 неточно: массивные ключи записаны как ``field1_3[0]['field4_0'].q2``, а в данных
эксперта это список объектов в ``field1_3``; у поставки товара нумерация заключения ``field5_*``, а не ``field6_*``;
итоговые флаги критериев (``field4_0`` …) в Excel отсутствуют. Поэтому состав ключей и тип значения берутся из данных.

Для каждого вида отчётных документов (checkType2: 13 — ``nir``, 14 — ``goods``, 15 — ``works``)
сохраняются ключи верхнего уровня (``top``) и поля элементов ``field1_3`` (``item``, путь вида ``field4_0.q2``)
с типом: ``bool`` — все непустые значения булевы, ``number`` — числа, ``list`` — списки, иначе ``text``.

Запуск::

    python -m scripts.derive_report_fields expertise_expert_opinion7s_1.csv
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

METHOD_BY_CHECK_TYPE2 = {"13": "nir", "14": "goods", "15": "works"}
MIN_COUNT = 3        # ключ, встретившийся реже, считается случайным
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge_store", "report_fields.json")


def kind_of(counter: Counter) -> str:
    """Тип значения по набору типов Python непустых значений.

    Args:
        counter: Счётчик имён типов значений (``bool``, ``int``, ``str`` …); ``NoneType`` игнорируется.

    Returns:
        str: ``bool`` / ``number`` / ``list`` / ``text``.
    """
    types = {t for t in counter if t != "NoneType"}
    if types == {"bool"}:
        return "bool"
    if types and types <= {"int", "float"}:
        return "number"
    if types == {"list"}:
        return "list"
    return "text"


def derive(csv_path: str) -> Dict[str, Dict[str, Any]]:
    """Собирает ключи и типы по выгрузке.

    Args:
        csv_path: Выгрузка ``expertise_expert_opinion7s`` (колонки ``checkType2``, ``data``).

    Returns:
        dict: ``вид → {"records": n, "top": {ключ: тип}, "item": {путь: тип}}``; ключи упорядочены.
    """
    csv.field_size_limit(sys.maxsize)
    top: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    item: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    seen: Dict[str, Counter] = defaultdict(Counter)
    records: Counter = Counter()
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            method = METHOD_BY_CHECK_TYPE2.get(row.get("checkType2") or "")
            if not method:
                continue
            try:
                data = json.loads(row.get("data") or "")
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            records[method] += 1
            for key, value in data.items():
                top[method][key][type(value).__name__] += 1
                seen[method][key] += 1
            for entry in data.get("field1_3") or []:
                for key, value in entry.items():
                    if isinstance(value, dict):
                        for sub, sub_value in value.items():
                            item[method][f"{key}.{sub}"][type(sub_value).__name__] += 1
                    else:
                        item[method][key][type(value).__name__] += 1
    result: Dict[str, Dict[str, Any]] = {}
    for method in sorted(records):
        result[method] = {
            "records": records[method],
            "top": {k: kind_of(c) for k, c in sorted(top[method].items(), key=lambda x: _order(x[0]))
                    if seen[method][k] >= MIN_COUNT},
            "item": {k: kind_of(c) for k, c in sorted(item[method].items(), key=lambda x: _order(x[0]))},
        }
    return result


def _order(key: str) -> List[Any]:
    """Ключ сортировки: числа в ``field4_10`` сравниваются как числа, а не как строки."""
    import re
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", key)]


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: читает выгрузку и пишет ``report_fields.json``."""
    parser = argparse.ArgumentParser(description="Ключи заключений объекта 7 по выгрузке экспертов")
    parser.add_argument("csv")
    parser.add_argument("--out", default=OUT_PATH)
    args = parser.parse_args(argv)
    result = derive(args.csv)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    for method, info in result.items():
        print(f"{method}: записей {info['records']}, ключей верхнего уровня {len(info['top'])}, полей элементов {len(info['item'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

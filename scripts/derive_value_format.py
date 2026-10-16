"""Определение формата значений заключений (``true/false`` или 1/0/2) по выгрузкам экспертов.

В формах аукциона, котировок и единственного поставщика большинство критериев разделов 2–4 эксперты хранят как
``true``/``false``, а в форме конкурса — числами 1/0/2. Чтобы сводное заключение выглядело как заключения экспертов,
формат определяется по данным: поле считается булевым, если среди непустых значений выгрузки **все** булевы
(и их не меньше :data:`MIN_VALUES`). Смешанные поля (булевы и числа вперемешку) остаются числовыми: в них эксперты
используют и «2» («не применимо»), которая булевым значением не выражается.

Результат — ``knowledge_store/value_formats.json`` вида ``{"<код формы>": {"bool": ["field2_2_1", ...]}}``.

Запуск::

    python -m scripts.derive_value_format "Отчеты и закупки поля.xlsx" \\
        44fz_auction_obj6=expertise_expert_opinion7s_4.csv 44fz_quotation_obj6=expertise_expert_opinion7s_7.csv \\
        44fz_single_supplier_obj6=expertise_expert_opinion7s_11.csv
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from scripts.import_forms import load_forms

OUT_PATH = Path(__file__).resolve().parent.parent / "knowledge_store" / "value_formats.json"
MIN_VALUES = 5           # меньше непустых значений — формат по данным не определить


def bool_keys(csv_path: str, field_keys: List[str]) -> List[str]:
    """Ключи, значения которых в выгрузке всегда булевы.

    Args:
        csv_path: Выгрузка ``expertise_expert_opinion7s`` одного способа закупки.
        field_keys: Ключи полей формы, которые проверяются.

    Returns:
        list[str]: Ключи, у которых не меньше :data:`MIN_VALUES` непустых значений и все они ``true``/``false``.
    """
    csv.field_size_limit(sys.maxsize)
    seen = defaultdict(lambda: [0, 0])          # ключ → [всего непустых, булевых]
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                data = json.loads(row.get("data") or "")
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            for key in field_keys:
                value = data.get(key)
                if value is None:
                    continue
                seen[key][0] += 1
                seen[key][1] += isinstance(value, bool)
    return sorted(k for k, (total, booleans) in seen.items() if total >= MIN_VALUES and total == booleans)


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: ``<xlsx> <код формы>=<csv> ...`` → запись :data:`OUT_PATH`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(__doc__)
        return 2
    registry = load_forms(args[0])
    result: Dict[str, dict] = {}
    for pair in args[1:]:
        form, path = pair.split("=", 1)
        keys = [f.field_key for f in registry[form] if not f.field_key.endswith("_text")]
        result[form] = {"bool": bool_keys(path, keys)}
        print(f"{form}: булевых полей {len(result[form]['bool'])}")
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

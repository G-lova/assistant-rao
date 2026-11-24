"""Экспорт названий полей из справочника ``fields_spravochnik*.xlsx`` в ``knowledge_store/form_labels.json``.

Запускается локально (не на проде): результат коммитится и уезжает в сервис вместе с кодом.

    python -m scripts.export_form_labels fields_spravochnik_v5.xlsx

В оверлей попадают только поля верхнего уровня (без точки в коде — вложенные ``rao_comment.*``, ``field1_3.name``
в сравнении критериев не участвуют) с непустым названием.
"""
import json
import sys

import openpyxl

from knowledge_store.form_labels import LABELS_PATH, service_form_codes

SHEET = "Поля"


def build(path: str) -> dict:
    """Читает лист «Поля» и собирает ``{код формы сервиса: {ключ поля: название}}``.

    Args:
        path: Путь к xlsx-справочнику (колонки: Тип формы, Объект, Код поля, Название поля, …).
    """
    ws = openpyxl.load_workbook(path, read_only=True)[SHEET]
    result: dict = {}
    for form_type, obj, key, label, *_ in list(ws.iter_rows(values_only=True))[1:]:
        if not (form_type and key and label) or "." in str(key):
            continue
        for code in service_form_codes(str(form_type), int(obj)):
            result.setdefault(code, {})[str(key)] = str(label).strip()
    return result


def main(argv: list) -> int:
    """Точка входа: пишет ``form_labels.json`` и печатает число полей по формам."""
    if len(argv) != 2:
        print(__doc__)
        return 1
    overlay = build(argv[1])
    LABELS_PATH.write_text(json.dumps(overlay, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for code, fields in sorted(overlay.items()):
        print(f"{code}: {len(fields)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

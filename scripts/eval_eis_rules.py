"""Оценка правил «XML извещения → раздел 1» по эталонным заключениям экспертов.

Для каждой экспертизы берётся последнее заключение формы «конкурс» из выгрузки
``expertise_expert_opinion7s`` и XML извещения (последняя версия) из папки экспертизы.
Сравниваются значения 1/0/2 по критериям раздела 1.

Запуск::

    python -m scripts.eval_eis_rules "Отчеты и закупки поля.xlsx" expertise_expert_opinion7s.csv expertises/
"""
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict

from knowledge_store import eis_notice
from scripts.import_forms import load_forms

FORM = "44fz_competition_obj6"


def norm(value):
    """Приводит значение поля к ``int`` 0/1/2 или ``None`` (значения бывают int и str)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number in (0, 1, 2) else None


def latest_opinions(csv_path: str) -> dict:
    """Последнее заполненное заключение формы «конкурс» по каждой экспертизе.

    Args:
        csv_path: Выгрузка ``expertise_expert_opinion7s``.

    Returns:
        dict: ``expertise_id → data``.
    """
    csv.field_size_limit(sys.maxsize)
    best = {}
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = row.get("data")
            if not raw or raw == "NULL":
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if "field2_2_1_1" not in data:
                continue
            key = row["expertise_id"]
            if key not in best or row["updated_at"] > best[key][0]:
                best[key] = (row["updated_at"], data)
    return {k: v[1] for k, v in best.items()}


def evaluate(xlsx: str, csv_path: str, root: str) -> str:
    """Сравнивает правила с эталоном и формирует отчёт.

    Args:
        xlsx: Путь к «Отчеты и закупки поля.xlsx».
        csv_path: Выгрузка заключений.
        root: Папка с подпапками экспертиз (в каждой — XML извещений).

    Returns:
        str: Отчёт по критериям: покрытие правилом, точность, типичные расхождения.
    """
    fields = [{"field_key": f.field_key, "label": f.label, "value_kind": f.value_kind}
              for f in load_forms(xlsx)[FORM]]
    opinions = latest_opinions(csv_path)
    stats = defaultdict(Counter)
    misses = defaultdict(list)
    used = 0
    for exp_id, data in sorted(opinions.items()):
        files = glob.glob(os.path.join(root, exp_id, "epNotification*.xml"))
        notice = eis_notice.latest_notice([open(f, "rb").read() for f in files])
        if not notice:
            continue
        used += 1
        for key, finding in eis_notice.evaluate_section1(notice, fields).items():
            truth = norm(data.get(key))
            if truth is None:
                continue
            if finding.value is None:
                stats[key]["не решено"] += 1
            elif finding.value == truth:
                stats[key]["верно"] += 1
            else:
                stats[key]["ошибка"] += 1
                misses[key].append(f"{exp_id}: правило={finding.value}, эксперт={truth}")
    label = {f["field_key"]: f["label"][:60] for f in fields}
    lines = [f"экспертиз с извещением и эталоном: {used}"]
    total = Counter()
    for key in sorted(stats, key=lambda k: [int(x) for x in k.split("_")[2:] if x.isdigit()]):
        c = stats[key]
        total.update(c)
        decided = c["верно"] + c["ошибка"]
        acc = f"{100 * c['верно'] / decided:.0f}%" if decided else "—"
        lines.append(f"{key:12} точность {acc:>5} (верно {c['верно']}, ошибок {c['ошибка']}, не решено {c['не решено']}) {label[key]}")
        if c["ошибка"]:
            lines.append("      " + "; ".join(misses[key][:4]))
    decided = total["верно"] + total["ошибка"]
    lines.append(f"ИТОГО: решено правилами {decided} из {sum(total.values())}, точность "
                 f"{100 * total['верно'] / max(decided, 1):.1f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    print(evaluate(*sys.argv[1:4]))

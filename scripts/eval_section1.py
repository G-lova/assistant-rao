"""Оценка правил раздела 1 («Наличие информации…») по эталонным заключениям для любого способа закупки.

В отличие от :mod:`scripts.eval_printform_rules` (только конкурс, ``view*.html``), скрипт:

* работает с любой формой справочника (``--form auction|quotation|single|competition``);
* ищет в папке экспертизы XML извещения (``epNotification*``) и печатные формы извещения (любое ``.html``);
* сопоставляет поля формы с правилами по названию критерия (:func:`knowledge_store.eis_notice.rule_for_label`),
  поэтому сдвиг нумерации в формах аукциона/котировок/единственного поставщика не мешает;
* берёт последнее заключение по каждой экспертизе из выгрузки эксперта (без фильтра по ключу формы).

Запуск (папки экспертиз — подпапки с именем ``expertise_id``)::

    python -m scripts.eval_section1 "Отчеты и закупки поля.xlsx" expertise_expert_opinion7s_4.csv aukciony/ --form auction
"""
import argparse
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from knowledge_store import eis_notice, eis_printform
from scripts.eval_eis_rules import norm
from scripts.eval_printform_rules import html_to_text
from scripts.import_forms import load_forms

FORM_BY_NAME = {"competition": "44fz_competition_obj6", "auction": "44fz_auction_obj6",
                "quotation": "44fz_quotation_obj6", "single": "44fz_single_supplier_obj6"}
MIN_KEYS = 40        # заключения короче — черновики, в эталон не берутся


def latest_any(csv_path: str) -> Dict[str, dict]:
    """Последнее по ``updated_at`` достаточно полное заключение по каждой экспертизе (форма определяется файлом).

    Args:
        csv_path: Выгрузка ``expertise_expert_opinion7s`` одного способа закупки.

    Returns:
        dict: ``expertise_id → data``.
    """
    csv.field_size_limit(sys.maxsize)
    best: Dict[str, tuple] = {}
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                data = json.loads(row.get("data") or "")
            except ValueError:
                continue
            if not isinstance(data, dict) or len(data) < MIN_KEYS:
                continue
            stamp = (row.get("updated_at") or "", int(row.get("id") or 0))
            if row["expertise_id"] not in best or stamp > best[row["expertise_id"]][0]:
                best[row["expertise_id"]] = (stamp, data)
    return {k: v[1] for k, v in best.items()}


def findings_for_dir(folder: str) -> Dict[str, "eis_notice.Finding"]:
    """Результаты правил по документам одной экспертизы: XML извещения приоритетнее печатной формы.

    Args:
        folder: Папка экспертизы.

    Returns:
        dict: ``номер правила (идентификатор) → Finding``; пусто, если извещения нет.
    """
    xmls = []
    for path in glob.glob(os.path.join(folder, "**", "*.xml"), recursive=True):
        raw = open(path, "rb").read()
        if b"epNotification" in raw[:4000]:
            xmls.append(raw)
    out: Dict[str, "eis_notice.Finding"] = {}
    notice = eis_notice.latest_notice(xmls) if xmls else None
    if notice:
        out.update({c: f for c, f in eis_notice.evaluate_all(notice).items() if f.value is not None})
    texts = []
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.htm*"), recursive=True)):
        text = html_to_text(path)
        if eis_printform.is_print_form(text):
            texts.append((path, text))
    if texts:
        _, text = texts[-1]
        for criterion, finding in eis_printform.evaluate_text(text).items():
            if finding.value is not None and criterion not in out:
                out[criterion] = finding
    return out


def evaluate(xlsx: str, csv_path: str, root: str, form: str, show: int = 6) -> str:
    """Сравнивает правила раздела 1 с эталоном и формирует отчёт.

    Args:
        xlsx: Файл «Отчеты и закупки поля.xlsx».
        csv_path: Выгрузка заключений экспертов по способу закупки.
        root: Папка с подпапками экспертиз.
        form: Имя формы (``auction``, ``quotation``, ``single``, ``competition``).
        show: Сколько ошибок показать по критерию.

    Returns:
        str: Текстовый отчёт.
    """
    fields = [f for f in load_forms(xlsx)[FORM_BY_NAME[form]]
              if f.field_key.startswith("field2_1_") and f.value_kind == "presence" and not f.field_key.endswith("_text")]
    opinions = latest_any(csv_path)
    stats: Dict[str, Counter] = defaultdict(Counter)
    misses: Dict[str, List[str]] = defaultdict(list)
    used = 0
    for exp_id in sorted(os.listdir(root)):
        folder = os.path.join(root, exp_id)
        if not os.path.isdir(folder) or exp_id not in opinions:
            continue
        findings = findings_for_dir(folder)
        if not findings:
            stats["—"]["без извещения"] += 1
            continue
        used += 1
        for field in fields:
            rule = eis_notice.rule_for_label(field.label)
            label = f"{field.field_key.replace('field', '')} {field.label[:60]}"
            truth = norm(opinions[exp_id].get(field.field_key))
            if truth is None:
                continue
            finding = findings.get(rule.criterion) if rule else None
            if not rule:
                stats[label]["нет правила"] += 1
            elif finding is None:
                stats[label]["не решено"] += 1
            elif finding.value == truth:
                stats[label]["верно"] += 1
            else:
                stats[label]["ошибка"] += 1
                misses[label].append(f"{exp_id}: правило={finding.value}, эксперт={truth}")
    lines = [f"форма {FORM_BY_NAME[form]}; экспертиз с извещением и эталоном: {used}"]
    total: Counter = Counter()
    for label in sorted(stats, key=lambda x: x):
        c = stats[label]
        if label == "—":
            lines.append(f"экспертиз без извещения: {c['без извещения']}")
            continue
        total.update(c)
        decided = c["верно"] + c["ошибка"]
        acc = f"{100 * c['верно'] / decided:.0f}%" if decided else "—"
        lines.append(f"{label:70} точность {acc:>4} (верно {c['верно']}, ошибок {c['ошибка']}, "
                     f"не решено {c['не решено']}, нет правила {c['нет правила']})")
        if c["ошибка"]:
            lines.append("      " + "; ".join(misses[label][:show]))
    decided = total["верно"] + total["ошибка"]
    lines.append(f"ИТОГО: решено {decided} из {sum(total.values())} сравнений, "
                 f"точность {100 * total['верно'] / max(decided, 1):.1f}%")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: разбирает аргументы и печатает отчёт."""
    parser = argparse.ArgumentParser(description="Оценка правил раздела 1 по эталону для любого способа закупки")
    parser.add_argument("xlsx")
    parser.add_argument("csv")
    parser.add_argument("root", help="папка с подпапками <expertise_id>")
    parser.add_argument("--form", choices=sorted(FORM_BY_NAME), default="competition")
    args = parser.parse_args(argv)
    print(evaluate(args.xlsx, args.csv, args.root, args.form))
    return 0


if __name__ == "__main__":
    sys.exit(main())

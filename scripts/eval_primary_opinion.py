"""Оценка проверок первичных заключений по истории: как коды замечаний связаны с оценкой ``range`` сводного эксперта.

Запуск::

    python -m scripts.eval_primary_opinion expertise_experts.csv expertise_expert_opinion7s.csv

Печатает по каждой группе (закупки / отчётные документы) долю заключений с каждым кодом замечания в разрезе оценок.
Код, у которого доля не растёт при снижении оценки, для её предсказания бесполезен.
Записи с пустым ``range`` пропускаются. ``is_benefit`` (эксперт-новичок) не исключается, но выводится отдельно.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from knowledge_store import primary_review

GRADES = ["0.00", "0.50", "0.75", "1.00"]


def load_pairs(experts_csv: str, opinions_csv: str) -> List[Tuple[str, dict, dict]]:
    """Склеивает оценки и заключения по ``(expertise_id, expert_id)``.

    Returns:
        list: Кортежи ``(группа, строка expertise_experts, data заключения)``; группа — ``report`` или ``procurement``.
    """
    csv.field_size_limit(sys.maxsize)
    rated: Dict[Tuple[str, str], dict] = {}
    with open(experts_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["range"] not in ("NULL", ""):
                rated[(row["expertise_id"], row["expert_id"])] = row
    out = []
    with open(opinions_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            meta = rated.get((row["expertise_id"], row["expert_id"]))
            if not meta:
                continue
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            if isinstance(data, dict):
                out.append(("report" if primary_review.is_report_data(data) else "procurement", meta, data))
    return out


def report(pairs: List[Tuple[str, dict, dict]]) -> None:
    """Печатает таблицу связи кодов замечаний с оценкой."""
    for group in ("procurement", "report"):
        sub = [(m, d) for g, m, d in pairs if g == group]
        if not sub:
            continue
        totals = Counter(f"{float(m['range']):.2f}" for m, _ in sub)
        codes: Dict[str, Counter] = defaultdict(Counter)
        for meta, data in sub:
            real = f"{float(meta['range']):.2f}"
            issues = primary_review.review_opinion(data)
            for code in {i.code for i in issues}:
                codes[code][real] += 1
        print(f"\n=== {group}: {len(sub)} заключений; по оценкам {dict(sorted(totals.items()))}")
        print(f"{'код':22}" + "".join(f"{g:>8}" for g in GRADES) + f"{'всего':>8}")
        for code, cnt in sorted(codes.items(), key=lambda x: -sum(x[1].values())):
            print(f"{code:22}" + "".join(f"{cnt[g] / totals[g]:8.1%}" if totals[g] else f"{'-':>8}" for g in GRADES)
                  + f"{sum(cnt.values()) / len(sub):8.1%}")

    benefit = Counter(m["is_benefit"] for _, m, _ in pairs)
    print(f"\nновички (is_benefit=1): {benefit['1']} из {len(pairs)}")


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description="Проверка первичных заключений по истории")
    parser.add_argument("experts_csv")
    parser.add_argument("opinions_csv")
    args = parser.parse_args(argv)
    report(load_pairs(args.experts_csv, args.opinions_csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

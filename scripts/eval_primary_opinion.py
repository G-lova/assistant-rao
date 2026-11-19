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

from configs import settings
from knowledge_store import primary_compare, primary_review

GRADES = ["0.00", "0.50", "0.75", "1.00"]


def is_test_expert(expert_id) -> bool:
    """Тестовый пользователь (``settings.PRIMARY_REVIEW_TEST_EXPERT_IDS``): его данные не используются для обучения и оценки."""
    try:
        return int(expert_id) in settings.PRIMARY_REVIEW_TEST_EXPERT_IDS
    except (TypeError, ValueError):
        return False


def load_pairs(experts_csv: str, opinions_csv: str) -> List[Tuple[str, dict, dict]]:
    """Склеивает оценки и заключения по ``(expertise_id, expert_id)``.

    Returns:
        list: Кортежи ``(группа, строка expertise_experts, data заключения)``; группа — ``report`` или ``procurement``.
    """
    csv.field_size_limit(sys.maxsize)
    rated: Dict[Tuple[str, str], dict] = {}
    with open(experts_csv, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if row["range"] not in ("NULL", "") and not is_test_expert(row["expert_id"]):
                rated[(row["expertise_id"], row["expert_id"])] = row
    out = []
    # errors="replace": оборванный при копировании хвост файла не роняет загрузку (строка с битым JSON пропускается)
    with open(opinions_csv, encoding="utf-8", errors="replace") as fh:
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


def load_summary_experts(users_csv: str) -> set:
    """Идентификаторы сводных экспертов (``users.role = 'raoExpert'``) из выгрузки ``users``."""
    csv.field_size_limit(sys.maxsize)
    with open(users_csv, encoding="utf-8") as fh:
        return {row["id"] for row in csv.DictReader(fh) if row["role"] == "raoExpert"}


def load_compare_cases(experts_csv: str, opinions_csv: str, summary_experts: Optional[set] = None) -> List[Tuple[dict, dict, dict]]:
    """Случаи для сравнения: оценённое заключение и итоговое сводное той же экспертизы.

    Итоговое — последнее по ``updated_at`` заключение сводного эксперта (``summary_experts`` — id с ролью ``raoExpert``);
    без списка ролей — последнее среди неоценённых заключений экспертизы (приближение: может попасть неоценённое
    заключение первичного эксперта).

    Returns:
        list: ``(строка expertise_experts, data первичного, data итогового)``.
    """
    csv.field_size_limit(sys.maxsize)
    meta: Dict[Tuple[str, str], dict] = {}
    with open(experts_csv, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            meta[(row["expertise_id"], row["expert_id"])] = row
    rated: Dict[str, List[Tuple[dict, dict]]] = defaultdict(list)
    final: Dict[str, Tuple[str, dict]] = {}
    # errors="replace": оборванный при копировании хвост файла не роняет загрузку (строка с битым JSON пропускается)
    with open(opinions_csv, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            m = meta.get((row["expertise_id"], row["expert_id"]))
            if not isinstance(data, dict) or is_test_expert(row["expert_id"]):
                continue            # заключения тестовых пользователей не берём ни как оцениваемые, ни как итоговые
            if m and m["range"] not in ("NULL", ""):
                rated[row["expertise_id"]].append((m, data))
            elif summary_experts is not None and row["expert_id"] not in summary_experts:
                continue            # итоговым может быть только заключение сводного эксперта
            elif row["expertise_id"] not in final or row["updated_at"] > final[row["expertise_id"]][0]:
                final[row["expertise_id"]] = (row["updated_at"], data)
    return [(m, d, final[e][1]) for e, items in rated.items() if e in final for m, d in items]


def report_compare(cases: List[Tuple[dict, dict, dict]]) -> None:
    """Печатает качество предложения оценки по расхождениям с итоговым заключением."""
    confusion: Counter = Counter()
    skipped = 0
    for meta, primary, summary in cases:
        cmp = primary_compare.compare(primary, summary)
        suggested = primary_compare.suggest_range(cmp)
        if suggested is None:
            skipped += 1
            continue
        confusion[(f"{suggested:.2f}", f"{float(meta['range']):.2f}")] += 1
    total = sum(confusion.values())
    print(f"\n=== сравнение с итоговым: {total} случаев (пропущено {skipped}: мало общих критериев)")
    print("предложено \\ реально " + "".join(f"{g:>8}" for g in GRADES))
    for sug in GRADES:
        print(f"{sug:22}" + "".join(f"{confusion[(sug, g)]:8}" for g in GRADES))
    exact = sum(c for (s, r), c in confusion.items() if s == r)
    near = sum(c for (s, r), c in confusion.items() if abs(GRADES.index(s) - GRADES.index(r)) <= 1)
    base = max(sum(confusion[(s, g)] for s in GRADES) for g in GRADES) / total
    print(f"точно {exact / total:.1%} (база «самая частая оценка» {base:.1%}), в пределах ступени {near / total:.1%}")


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
    parser.add_argument("--users", help="users.csv: итоговым считать заключение сводного эксперта (role = raoExpert)")
    parser.add_argument("--compare", action="store_true", help="качество оценки по расхождениям с итоговым заключением")
    args = parser.parse_args(argv)
    if args.compare:
        report_compare(load_compare_cases(args.experts_csv, args.opinions_csv,
                                          load_summary_experts(args.users) if args.users else None))
        return 0
    report(load_pairs(args.experts_csv, args.opinions_csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

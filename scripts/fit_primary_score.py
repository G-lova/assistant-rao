"""Обучает веса расхождений и пороги процента для оценки первичных экспертов (``knowledge_store/primary_score_model.json``).

Веса — гребневая регрессия оценки на индикаторы расхождений (``вид:ключ``, как в
:func:`primary_compare.features`): чем больше вес, тем сильнее расхождение понижает оценку. Веса знаковые: отрицательный вес
означает, что оценивающие это расхождение фактически «прощают» (оценка при нём чаще выше). Ограничение «только положительные» на
истории стоит закупкам ~5 п. п. точности (51% против 57%). Затем считается взвешенный
процент сходства (:mod:`knowledge_store.primary_score`) и подбираются три порога, максимизирующие точность. Качество — кросс-валидация
по экспертизам (веса и пороги подбираются только по обучающей части).

Запуск::

    python -m scripts.fit_primary_score expertise_experts.csv expertise_expert_opinion7s.csv --users users.csv
"""
import argparse
import itertools
import json
import sys
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from knowledge_store import primary_compare, primary_score
from scripts.eval_primary_opinion import load_compare_cases, load_summary_experts

GRADES = list(primary_score.GRADES)


def fit_weights(rows: Sequence[Tuple[dict, int]], names: List[str], alpha: float) -> Dict[str, float]:
    """Веса расхождений: оценка падает при расхождении, поэтому регрессия идёт на «минус оценку».

    Args:
        rows: ``(индикаторы расхождений {имя: 1}, индекс оценки 0..3)``.
        names: Имена признаков (``вид:ключ``).
        alpha: Сила регуляризации.

    Returns:
        dict: ``имя → вес`` для всех признаков (в том числе нулевых: у них вес «не влияет», а не «средний»).
    """
    import numpy as np
    from sklearn.linear_model import Ridge

    x = np.array([[ind.get(n, 0.0) for n in names] for ind, _ in rows])
    y = -np.array([g for _, g in rows], dtype=float)
    model = Ridge(alpha=alpha).fit(x, y)
    return {n: round(float(w), 5) for n, w in zip(names, model.coef_)}


def make_spec(weights: Dict[str, float]) -> dict:
    """Параметры группы без порогов: нормировка ``w_avg`` и вес по умолчанию ``w_default`` (средний положительный вес)."""
    positive = [w for w in weights.values() if w > 1e-6]
    mean = sum(positive) / max(len(positive), 1)
    return {"weights": weights, "w_avg": round(mean, 5), "w_default": round(mean, 5), "thresholds": [0.0, 0.0, 0.0]}


def fit_thresholds(percents: Sequence[float], y: Sequence[int], grid: int = 40) -> List[float]:
    """Три порога процента (0 | 0.5 | 0.75 | 1.0), максимизирующие точность на обучении (сетка по квантилям)."""
    import numpy as np

    p, y = np.asarray(percents), np.asarray(y)
    cand = np.unique(np.quantile(p, np.linspace(0.02, 0.98, grid)))
    best, best_t = -1.0, (0.0, 0.0, 0.0)
    for t in itertools.combinations(cand, 3):
        acc = (np.searchsorted(t, p, side="right") == y).mean()
        if acc > best:
            best, best_t = acc, t
    return [round(float(v), 1) for v in best_t]


def percent_of(case, spec: dict) -> float:
    """Взвешенный процент сходства случая (``(common, [Difference])``) по параметрам ``spec``."""
    return primary_score.weighted_percent(case, spec)["percent"]


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: подбор alpha, кросс-валидация, запись JSON."""
    import numpy as np
    from sklearn.model_selection import GroupKFold

    parser = argparse.ArgumentParser(description="Обучение весов расхождений и порогов процента")
    parser.add_argument("experts_csv")
    parser.add_argument("opinions_csv")
    parser.add_argument("--users")
    parser.add_argument("--min-count", type=int, default=10)
    parser.add_argument("--alphas", default="30,100", help="кандидаты alpha: лучший по кросс-валидации записывается")
    parser.add_argument("--out", default=primary_score.MODEL_PATH)
    args = parser.parse_args(argv)

    cases = load_compare_cases(args.experts_csv, args.opinions_csv, load_summary_experts(args.users) if args.users else None)
    model = {}
    for group in ("procurement", "report"):
        items = []
        for meta, primary, summary in cases:
            cmp = primary_compare.compare(primary, summary)
            if cmp.group == group and cmp.common >= 5:
                ind = {f"{d.kind}:{d.key}": 1.0 for d in cmp.differences}
                items.append((meta["expertise_id"], GRADES.index(float(meta["range"])), ind, cmp))
        counts = Counter(n for _, _, ind, _ in items for n in ind)
        names = sorted(n for n, c in counts.items() if c >= args.min_count)
        y = np.array([g for _, g, _, _ in items])
        groups = [e for e, _, _, _ in items]
        best = None
        for alpha in (float(a) for a in args.alphas.split(",")):
            pred = np.zeros(len(y), int)
            for tr, te in GroupKFold(5).split(y, y, groups):
                spec = make_spec(fit_weights([(items[i][2], items[i][1]) for i in tr], names, alpha))
                spec["thresholds"] = fit_thresholds([percent_of(items[i][3], spec) for i in tr], y[tr])
                pred[te] = [sum(1 for t in spec["thresholds"] if percent_of(items[i][3], spec) >= t) for i in te]
            acc = (pred == y).mean()
            print(f"{group}: alpha={alpha:g} точно {acc:.1%}, в пределах ступени {(abs(pred - y) <= 1).mean():.1%}")
            if best is None or acc > best[0]:
                best = (acc, alpha)
        spec = make_spec(fit_weights([(ind, g) for _, g, ind, _ in items], names, best[1]))
        spec["thresholds"] = fit_thresholds([percent_of(c, spec) for _, _, _, c in items], y)
        spec.update({"alpha": best[1], "cases": len(items), "cv_exact": round(float(best[0]), 3)})
        print(f"{group}: {len(items)} случаев, весов {len(spec['weights'])}, w_avg {spec['w_avg']}, пороги процента {spec['thresholds']}")
        model[group] = spec
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

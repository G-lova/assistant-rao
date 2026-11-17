"""Обучает модель оценки первичных экспертов и пишет коэффициенты в ``knowledge_store/primary_range_model.json``.

Многоклассовая логистическая регрессия на признаках :func:`knowledge_store.primary_compare.features`, отдельно для
закупок (``procurement``) и отчётных документов (``report``). Качество считается кросс-валидацией по экспертизам
(одна экспертиза не попадает одновременно в обучение и проверку). Редкие признаки (меньше ``--min-count`` случаев)
отбрасываются: файл остаётся небольшим, а вывод — чистым Python (sklearn нужен только для обучения).

Запуск::

    python -m scripts.fit_primary_range expertise_experts.csv expertise_expert_opinion7s.csv
"""
import argparse
import json
import sys
from collections import Counter
from typing import List, Optional

from knowledge_store import primary_compare, primary_review
from scripts.eval_primary_opinion import load_compare_cases

GRADES = [0.0, 0.5, 0.75, 1.0]
STRONG_C = 0.05      # сильная регуляризация: признаков-критериев много, примеров на критерий мало


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: обучение, кросс-валидация, запись JSON."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold

    parser = argparse.ArgumentParser(description="Обучение модели оценки первичных экспертов")
    parser.add_argument("experts_csv")
    parser.add_argument("opinions_csv")
    parser.add_argument("--min-count", type=int, default=10)
    parser.add_argument("--out", default=primary_compare.MODEL_PATH)
    args = parser.parse_args(argv)

    cases = load_compare_cases(args.experts_csv, args.opinions_csv)
    model = {}
    for group in ("procurement", "report"):
        rows = []
        for meta, primary, summary in cases:
            cmp = primary_compare.compare(primary, summary)
            if cmp.group == group and cmp.common >= 5:
                rows.append((meta["expertise_id"], float(meta["range"]), primary_compare.features(primary, summary, cmp)))
        counts = Counter(name for _, _, f in rows for name in f)
        names = sorted(n for n, c in counts.items() if c >= args.min_count)
        x = np.array([[f.get(n, 0.0) for n in names] for _, _, f in rows])
        y = np.array([GRADES.index(r) for _, r, _ in rows])
        mean, scale = x.mean(0), np.maximum(x.std(0), 1e-6)   # константный признак: масштаб не 0
        z = (x - mean) / scale
        pred = np.zeros(len(y), int)
        for train, test in GroupKFold(5).split(z, y, [g for g, _, _ in rows]):
            pred[test] = LogisticRegression(C=STRONG_C, max_iter=3000).fit(z[train], y[train]).predict(z[test])
        base = np.bincount(y).max() / len(y)
        print(f"{group}: {len(rows)} случаев, признаков {len(names)}; кросс-валидация: точно {(pred == y).mean():.1%} "
              f"(база {base:.1%}), в пределах ступени {(abs(pred - y) <= 1).mean():.1%}")
        clf = LogisticRegression(C=STRONG_C, max_iter=3000).fit(z, y)
        model[group] = {"features": names, "mean": [round(float(v), 6) for v in mean],
                        "scale": [round(float(v), 6) for v in scale],
                        "coef": [[round(float(v), 5) for v in row] for row in clf.coef_],
                        "intercept": [round(float(v), 5) for v in clf.intercept_],
                        "classes": [GRADES[int(c)] for c in clf.classes_], "cases": len(rows),
                        "cv_exact": round(float((pred == y).mean()), 3)}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

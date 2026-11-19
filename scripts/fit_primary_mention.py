"""Обучает модель «упомянет ли сводный эксперт расхождение» по новым комментариям (``knowledge_store/primary_mention_model.json``).

Разметка: в комментариях с июля 2026 критерии названы номерами («2.1.5. Название: …»). Для каждого расхождения первичного с
итоговым (закупки) метка 1, если номер критерия есть в комментарии; случаи с шаблоном «ЭЗ соответствует …» без номеров дают
метки 0. Номер критерия берётся из названия поля справочника форм (``--forms``). Качество — кросс-валидация по заключениям.

Запуск::

    python -m scripts.fit_primary_mention expertise_experts.csv expertise_expert_opinion7s.csv --users users.csv --forms forms.xlsx
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from knowledge_store import primary_compare, primary_mention
from scripts import import_forms
from scripts.eval_primary_opinion import load_compare_cases, load_summary_experts

# строка комментария по критерию: «2.1.5. Название критерия: текст»
CRITERION_RE = re.compile(r"(?m)^\s*(\d+(?:\.\d+){1,3})\.\s+[^\n:]{8,300}?:\s")
SINCE = "2026-07-01"               # с этой даты комментарии пишутся по критериям
MIN_COUNT = 5                     # признак «вид:ключ» должен встретиться не меньше раз


def key_numbers(forms_xlsx: str) -> Dict[str, str]:
    """``ключ поля → номер критерия`` из названий полей справочника форм («2.1.5 Название …»)."""
    counts: Dict[str, Counter] = defaultdict(Counter)
    for fields in import_forms.load_forms(forms_xlsx).values():
        for f in fields:
            m = re.match(r"\s*(\d+(?:\.\d+)*)\.?\s", f.label or "")
            if m:
                counts[f.field_key][m.group(1)] += 1
    return {k: c.most_common(1)[0][0] for k, c in counts.items()}


def mentioned_numbers(comment: str) -> Optional[set]:
    """Номера критериев из комментария; ``None``, если комментарий не размечается (нет ни номеров, ни шаблона «ЭЗ соответствует»)."""
    found = set(CRITERION_RE.findall(comment or ""))
    return found if found or "ЭЗ соответствует" in (comment or "") else None


def build_rows(cases, numbers: Dict[str, str]) -> List[Tuple[str, str, str, int, int]]:
    """Строки обучения ``(заключение, вид:ключ, вид, метка, число расхождений)`` по закупочным случаям с июля 2026."""
    rows = []
    for meta, primary, summary in cases:
        if meta["range"] in ("NULL", "") or isinstance(primary.get("field1_3"), list) or meta["updated_at"] < SINCE:
            continue
        cmp = primary_compare.compare(primary, summary)
        real = mentioned_numbers(meta["comment"])
        if cmp.group != "procurement" or real is None:
            continue
        for d in cmp.differences:
            rows.append((f"{meta['expertise_id']}_{meta['expert_id']}", f"{d.kind}:{d.key}", d.kind,
                         int(numbers.get(d.key) in real), len(cmp.differences)))
    return rows


def features(rows, names: List[str]):
    """Матрица признаков: ``вид:ключ`` (one-hot), число расхождений / 20, вид расхождения."""
    import numpy as np

    idx = {n: j for j, n in enumerate(names)}
    kinds = ("missed", "false_alarm", "other")
    x = np.zeros((len(rows), len(names) + 1 + len(kinds)))
    for r, (_, name, kind, _, n) in enumerate(rows):
        if name in idx:
            x[r, idx[name]] = 1
        x[r, len(names)] = min(n, primary_mention.MAX_DIFFERENCES) / 20
        x[r, len(names) + 1 + kinds.index(kind)] = 1
    return x


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: разметка, кросс-валидация, запись JSON."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("experts_csv")
    parser.add_argument("opinions_csv")
    parser.add_argument("--users")
    parser.add_argument("--forms", required=True, help="справочник форм (xlsx) — номера критериев")
    parser.add_argument("--c", type=float, default=0.3, help="обратная сила регуляризации")
    parser.add_argument("--min-probability", type=float, default=0.4)
    parser.add_argument("--out", default=primary_mention.MODEL_PATH)
    args = parser.parse_args(argv)

    cases = load_compare_cases(args.experts_csv, args.opinions_csv, load_summary_experts(args.users) if args.users else None)
    rows = build_rows(cases, key_numbers(args.forms))
    counts = Counter(name for _, name, _, _, _ in rows)
    names = sorted(n for n, c in counts.items() if c >= MIN_COUNT)
    x, y, groups = features(rows, names), np.array([r[3] for r in rows]), [r[0] for r in rows]
    prob = np.zeros(len(y))
    for tr, te in GroupKFold(5).split(x, y, groups):
        prob[te] = LogisticRegression(C=args.c, max_iter=500).fit(x[tr], y[tr]).predict_proba(x[te])[:, 1]
    keep = prob >= args.min_probability
    print(f"строк {len(rows)}, доля упомянутых {y.mean():.1%}, AUC {roc_auc_score(y, prob):.3f}; при ≥ {args.min_probability}: "
          f"оставлено {keep.mean():.0%}, точность {y[keep].mean():.1%}, полнота от найденных {y[keep].sum() / y.sum():.1%}")
    model = LogisticRegression(C=args.c, max_iter=500).fit(x, y)
    coef = model.coef_[0]
    spec = {"weights": {n: round(float(coef[j]), 4) for j, n in enumerate(names)}, "bias": round(float(model.intercept_[0]), 4),
            "n_coef": round(float(coef[len(names)]), 4),
            "kind_coef": {k: round(float(coef[len(names) + 1 + i]), 4) for i, k in enumerate(("missed", "false_alarm", "other"))},
            "min_probability": args.min_probability, "cases": len({r[0] for r in rows}), "auc": round(float(roc_auc_score(y, prob)), 3)}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"procurement": spec}, fh, ensure_ascii=False)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

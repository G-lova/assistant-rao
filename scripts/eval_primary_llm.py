"""Проверяет на истории замечания модели «Недостаточная аргументация / Противоречия / Формальные недостатки».

Для выборки заключений (оценка < 1, есть комментарий сводного) запускает :func:`knowledge_store.primary_llm_review.review` и сравнивает
с темами в комментарии сводного эксперта (по ключевым фразам): точность (доля заключений, где найденная категория есть в комментарии),
базовая доля темы в выборке и полнота. Категорию стоит подключать, если точность заметно выше базовой доли.

Запуск на dev (нужен доступ к модели, как у остальных задач сервиса)::

    python -m scripts.eval_primary_llm expertise_experts.csv expertise_expert_opinion7s.csv --users users.csv --count 300 --out llm_review.csv
"""
import argparse
import asyncio
import csv
import json
import random
import re
import sys
from collections import Counter
from typing import Dict, List, Optional

from knowledge_store import primary_llm_review
from scripts.eval_primary_opinion import load_compare_cases, load_summary_experts

# тема комментария сводного → категория таксономии (ключевые фразы, как в анализе комментариев)
TOPICS: Dict[str, "re.Pattern"] = {
    "ARGUMENTATION": re.compile(r"не аргумент|не обоснов|без обоснован|не раскры|не указан\w* (?:пункт|ссылк)|нет ссылк|отсутствует ссылк|субъектив|голослов", re.I),
    "CONTRADICTION": re.compile(r"противореч|вывод\w*\s+не\s+(?:соответств|согласу)|не\s+согласу\w+\s+с\s+вывод", re.I),
    "FORMAL": re.compile(r"оформлен|опечат|орфограф|стилист|терминолог|структур|нумерац", re.I),
}


def topics_of(comment: str) -> set:
    """Категории, темы которых есть в комментарии сводного эксперта."""
    return {code for code, rx in TOPICS.items() if rx.search(comment or "")}


def diagnostics(rows: List[dict]) -> Dict[str, int]:
    """Где теряются замечания: заключений с текстом, ответов с замечаниями, замечаний до и после проверки цитат."""
    traces = [r.get("trace") or {} for r in rows]
    return {"with_text": sum(1 for t in traces if t.get("has_text")),
            "answers_with_findings": sum(1 for t in traces if t.get("raw")),
            "raw_findings": sum(len(t.get("raw") or []) for t in traces),
            "verified_findings": sum(len(t.get("verified") or []) for t in traces)}


def score(rows: List[dict]) -> Dict[str, dict]:
    """Точность / полнота / базовая доля по категориям.

    Args:
        rows: ``{"found": set категорий модели, "topics": set тем комментария}`` по заключениям.

    Returns:
        dict: ``категория → {flagged, precision, recall, base}``.
    """
    result = {}
    n = max(len(rows), 1)
    for code in primary_llm_review.CATEGORY_HINTS:
        flagged = [r for r in rows if code in r["found"]]
        topical = [r for r in rows if code in r["topics"]]
        hits = [r for r in flagged if code in r["topics"]]
        result[code] = {"flagged": len(flagged), "base": round(len(topical) / n, 3),
                        "precision": round(len(hits) / len(flagged), 3) if flagged else None,
                        "recall": round(len(hits) / len(topical), 3) if topical else None}
    return result


async def run(cases, llm, count: int, seed: int = 1, debug: int = 0) -> List[dict]:
    """Прогоняет модель по выборке заключений (оценка < 1, непустой комментарий).

    Args:
        debug: Сколько первых ответов модели (до проверки цитат) напечатать в stderr.
    """
    pool = [(m, p, s) for m, p, s in cases if m["range"] not in ("NULL", "") and float(m["range"]) < 1
            and m.get("comment") not in (None, "", "NULL")]
    random.Random(seed).shuffle(pool)
    rows = []
    for meta, primary, summary in pool[:count]:
        trace: dict = {}
        try:
            findings = await primary_llm_review.review(llm, primary, summary, trace=trace)
        except Exception as exc:  # noqa: BLE001 — один сбой модели не должен останавливать прогон
            print(f"экспертиза {meta['expertise_id']}: {exc}", file=sys.stderr)
            continue
        rows.append({"expertise_id": meta["expertise_id"], "expert_id": meta["expert_id"], "range": meta["range"],
                     "found": {f["category"] for f in findings}, "topics": topics_of(meta["comment"]), "findings": findings,
                     "trace": trace})
        if debug and len(rows) <= debug:
            print(f"[debug {meta['expertise_id']}/{meta['expert_id']}] текст: {trace.get('has_text')}; ответ модели: "
                  f"{json.dumps(trace.get('raw'), ensure_ascii=False)[:600]}; после проверки цитат: {len(trace.get('verified') or [])}",
                  file=sys.stderr)
    return rows


async def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: прогон, сводка, CSV с найденными замечаниями."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("experts_csv")
    parser.add_argument("opinions_csv")
    parser.add_argument("--users")
    parser.add_argument("--count", type=int, default=300, help="сколько заключений проверить")
    parser.add_argument("--out", default="llm_review.csv")
    parser.add_argument("--debug", type=int, default=0, help="напечатать первые N ответов модели до проверки цитат")
    args = parser.parse_args(argv)

    from configs.llm_client import get_llm
    from risk_monitoring.llm_json import LLMJson

    client, model = get_llm()
    llm = LLMJson(client, model, rate=1.0, concurrency=2)
    cases = load_compare_cases(args.experts_csv, args.opinions_csv, load_summary_experts(args.users) if args.users else None)
    rows = await run(cases, llm, args.count, debug=args.debug)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["expertise_id", "expert_id", "range", "category", "quote", "explanation"])
        for r in rows:
            for f in r["findings"]:
                writer.writerow([r["expertise_id"], r["expert_id"], r["range"], f["category"], f["quote"], f["explanation"]])
    print(json.dumps({"checked": len(rows), "diagnostics": diagnostics(rows), "by_category": score(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

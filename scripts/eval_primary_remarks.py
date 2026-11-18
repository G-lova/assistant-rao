"""Проверка кодов замечаний по отчётным документам на истории: совпадение с темами реальных комментариев сводных экспертов.

Запуск::

    python -m scripts.eval_primary_remarks expertise_experts.csv expertise_expert_opinion7s.csv --users users.csv

Для каждого кода печатает: долю заключений с кодом, долю среди них комментариев с соответствующей темой (точность)
и долю комментариев с темой, у которых код сработал (полнота). Тема определяется по ключевым фразам комментария.
"""
import argparse
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional

from knowledge_store import primary_remarks
from scripts import eval_primary_opinion as ev

# Тема комментария → (регулярное выражение по тексту комментария, коды, которые должны её покрывать)
THEMES = {
    "перечень документов": (re.compile(r"перечень документов|не указан\w* документ|реквизит", re.I), {"DOCS_MISSING", "DOCS_EXTRA", "DOCS_DATE", "DOCS_NO_REQUISITES", "DOCS_EMPTY"}),
    "требования к составу": (re.compile(r"требовани\w+ к составу|состав\w* (?:представления )?отчётн|состав\w* отчетн", re.I), {"REQ_MISSING", "REQ_EXTRA", "REQ_EMPTY", "REQ_NO_REFS"}),
    "предмет экспертизы": (re.compile(r"предмет\w* экспертизы", re.I), {"SUBJECT_EMPTY", "SUBJECT_IS_CONCLUSION", "SUBJECT_REQUISITES"}),
    "вывод": (re.compile(r"вывод\w*", re.I), {"CONCLUSION_POLARITY"}),
}


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: считает точность и полноту кодов."""
    parser = argparse.ArgumentParser(description="Коды замечаний по отчётным документам на истории")
    parser.add_argument("experts_csv")
    parser.add_argument("opinions_csv")
    parser.add_argument("--users")
    args = parser.parse_args(argv)
    cases = ev.load_compare_cases(args.experts_csv, args.opinions_csv, ev.load_summary_experts(args.users) if args.users else None)
    cases = [c for c in cases if c[1].get("field1_3") is not None and isinstance(c[1].get("field1_3"), list)]
    fired: Dict[str, int] = defaultdict(int)
    theme_hit: Dict[str, int] = defaultdict(int)       # тема есть, и код из её группы сработал
    theme_all: Dict[str, int] = defaultdict(int)       # тема есть в комментарии
    code_theme: Dict[str, int] = defaultdict(int)      # код сработал и тема есть
    by_grade: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for meta, primary, summary in cases:
        codes = {r.code for r in primary_remarks.review_report(primary, summary)}
        comment = meta.get("comment") or ""
        grade = f"{float(meta['range']):.2f}" if meta["range"] not in ("NULL", "") else "-"
        for code in codes:
            fired[code] += 1
            by_grade[code][grade] += 1
        for theme, (rx, group) in THEMES.items():
            has = bool(rx.search(comment))
            theme_all[theme] += has
            if has and codes & group:
                theme_hit[theme] += 1
            for code in codes & group:
                code_theme[code] += has
    print(f"отчётных случаев: {len(cases)}")
    for theme, (_, group) in THEMES.items():
        print(f"\n[{theme}] комментариев с темой {theme_all[theme]}; полнота {theme_hit[theme] / max(theme_all[theme], 1):.1%}")
        for code in sorted(group):
            if fired[code]:
                print(f"  {code:22} срабатывает {fired[code]:5} ({fired[code] / len(cases):.1%}), точность {code_theme[code] / fired[code]:.1%}, по оценкам {dict(sorted(by_grade[code].items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Оценка работы конвейера ``/generate-summary-opinion`` по эталонным заключениям экспертов.

Сравнивает JSON-ответы конвейера (по одному файлу ``<expertise_id>.json`` с ключами ``result.data`` и
``result.trace``) с последним заключением эксперта из выгрузки ``expertise_expert_opinion7s``. Считает
покрытие, согласие по значениям 1/0/2, точность и полноту «0», качество по статусам и источникам
(``verified`` / ``proposed`` / …), точность общих полей и сравнение замечаний.

Запуск::

    python -m scripts.eval_generated "Отчеты и закупки поля.xlsx" expertise_expert_opinion7s_rao.csv generated/ [--json out.json]
"""
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

from knowledge_store import eis_notice
from scripts.eval_eis_rules import FORM, latest_opinions, norm
from scripts.import_forms import load_forms

OUTLIERS = {"5162", "5263", "2403"}      # выброс (всё «0»), тестовая и неполная экспертизы
NUMBER_RE = re.compile(r"(?<![\d.])(\d\.\d{1,2}(?:\.\d{1,2})?)(?![\d.])")


def load_generated(directory: str) -> dict:
    """Читает ответы конвейера: ``expertise_id → {"data", "trace", "stats"}``.

    Args:
        directory: Папка с файлами ``<id>.json`` (ответ эндпоинта целиком или только ``result``).
    """
    out = {}
    for path in glob.glob(os.path.join(directory, "*.json")):
        raw = json.load(open(path, encoding="utf-8"))
        result = raw.get("result", raw)
        out[os.path.basename(path)[:-5]] = {"data": result.get("data") or {}, "trace": result.get("trace") or {},
                                             "stats": result.get("stats") or {}}
    return out


def pct(part: int, whole: int) -> str:
    """Доля в процентах строкой (``—``, если знаменатель 0)."""
    return f"{100 * part / whole:.0f}%" if whole else "—"


def evaluate(xlsx: str, csv_path: str, directory: str) -> dict:
    """Считает метрики и возвращает их словарём (его же печатает :func:`report`)."""
    fields = load_forms(xlsx)[FORM]
    leaf = [f for f in fields if f.value_kind in ("presence", "compliance")]
    label = {f.field_key: f.label for f in fields}
    opinions = latest_opinions(csv_path)
    generated = load_generated(directory)
    ids = sorted(i for i in generated if i in opinions and i not in OUTLIERS)

    pairs, per_key, per_status, per_source = [], defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    cover = Counter()
    for exp_id in ids:
        data, trace, expert = generated[exp_id]["data"], generated[exp_id]["trace"], opinions[exp_id]
        for f in leaf:
            ours, theirs = norm(data.get(f.field_key)), norm(expert.get(f.field_key))
            entry = trace.get(f.field_key) or {}
            status, source = entry.get("status") or "none", entry.get("source") or entry.get("rule") or "-"
            cover["expert_filled"] += theirs is not None
            cover["ours_filled"] += ours is not None
            cover["both"] += ours is not None and theirs is not None
            if ours is None and theirs is not None:
                cover["missed"] += 1
                per_key[f.field_key]["missed"] += 1
            if ours is None or theirs is None:
                continue
            pairs.append((exp_id, f.field_key, ours, theirs, status, source))
            per_key[f.field_key]["n"] += 1
            per_key[f.field_key]["ok"] += ours == theirs
            per_status[status]["n"] += 1
            per_status[status]["ok"] += ours == theirs
            per_source[source]["n"] += 1
            per_source[source]["ok"] += ours == theirs
    confusion = Counter((o, t) for _, _, o, t, _, _ in pairs)
    zeros_ours = [p for p in pairs if p[2] == 0]
    zeros_exp = [p for p in pairs if p[3] == 0]
    sections = defaultdict(Counter)
    for exp_id, key, ours, theirs, status, source in pairs:
        section = re.match(r"field2_(\d)", key)
        name = f"раздел {section.group(1)}" if section else "прочее"
        sections[name]["n"] += 1
        sections[name]["ok"] += ours == theirs

    # общие поля
    general = defaultdict(Counter)
    for exp_id in ids:
        data, expert = generated[exp_id]["data"], opinions[exp_id]
        for key in ("field1_1", "field1_2", "field1_2_unit", "field1_3", "field2_2_2_0"):
            ours, theirs = data.get(key), expert.get(key)
            general[key]["generated"] += ours not in (None, "")
            general[key]["expert"] += theirs not in (None, "")
            if ours in (None, "") or theirs in (None, ""):
                continue
            general[key]["both"] += 1
            if key == "field1_1":
                same = abs(float(ours) - float(theirs)) < 0.01
            elif key in ("field1_2",):
                same = abs(float(ours) - float(theirs)) < 0.01
            elif key == "field2_2_2_0":
                same = str(ours) == str(theirs)
            else:
                same = str(ours).strip().casefold() == str(theirs).strip().casefold()
            general[key]["same"] += same

    # замечания в field3: какие номера критериев упомянуты
    cited = Counter()
    for exp_id in ids:
        text = generated[exp_id]["data"].get("field3") or ""
        ours = set(NUMBER_RE.findall(text))
        number_by_key = {f.field_key: eis_notice.criterion_number(f.label) for f in leaf}
        theirs = {number_by_key[f.field_key] for f in leaf if norm(opinions[exp_id].get(f.field_key)) == 0}
        theirs.discard(None)
        cited["ours"] += len(ours)
        cited["experts_zero"] += len(theirs)
        cited["hit"] += len(ours & theirs)
    return {"ids": ids, "cover": cover, "pairs": len(pairs), "agree": sum(p[2] == p[3] for p in pairs),
            "confusion": confusion, "zeros_ours": len(zeros_ours), "zeros_ours_hit": sum(p[3] == 0 for p in zeros_ours),
            "zeros_exp": len(zeros_exp), "zeros_exp_found": sum(p[2] == 0 for p in zeros_exp), "per_key": per_key,
            "per_status": per_status, "per_source": per_source, "sections": sections, "general": general,
            "cited": cited, "label": label, "pairs_list": pairs, "number": {f.field_key: eis_notice.criterion_number(f.label) for f in leaf}}


def report(res: dict) -> str:
    """Текстовый отчёт по результатам :func:`evaluate`."""
    c, lines = res["cover"], []
    lines.append(f"Экспертиз в оценке: {len(res['ids'])} (без выбросов {sorted(OUTLIERS)})")
    lines.append(f"Критериев у эксперта заполнено: {c['expert_filled']}; у нас: {c['ours_filled']}; в обоих: {c['both']}; "
                 f"пропущено нами (эксперт заполнил): {c['missed']}")
    lines.append(f"Согласие по значениям: {res['agree']}/{res['pairs']} = {pct(res['agree'], res['pairs'])}")
    lines.append("\nМатрица (наше → эксперта): " + ", ".join(f"{o}→{t}: {n}" for (o, t), n in sorted(res["confusion"].items())))
    lines.append(f"\nНаши «0»: {res['zeros_ours']}, из них подтвердил эксперт: {res['zeros_ours_hit']} "
                 f"(точность {pct(res['zeros_ours_hit'], res['zeros_ours'])})")
    lines.append(f"«0» экспертов: {res['zeros_exp']}, найдено нами: {res['zeros_exp_found']} "
                 f"(полнота {pct(res['zeros_exp_found'], res['zeros_exp'])})")
    lines.append("\nПо разделам:")
    for name, v in sorted(res["sections"].items()):
        lines.append(f"  {name}: {v['ok']}/{v['n']} = {pct(v['ok'], v['n'])}")
    lines.append("\nПо статусу trace:")
    for name, v in sorted(res["per_status"].items(), key=lambda x: -x[1]["n"]):
        lines.append(f"  {name}: {v['ok']}/{v['n']} = {pct(v['ok'], v['n'])}")
    lines.append("\nПо источнику:")
    for name, v in sorted(res["per_source"].items(), key=lambda x: -x[1]["n"]):
        lines.append(f"  {name}: {v['ok']}/{v['n']} = {pct(v['ok'], v['n'])}")
    lines.append("\nОбщие поля (есть у нас / у эксперта / в обоих / совпало):")
    for key, v in res["general"].items():
        lines.append(f"  {key}: {v['generated']} / {v['expert']} / {v['both']} / {v['same']}")
    k = res["cited"]
    lines.append(f"\nfield3: упомянуто номеров критериев {k['ours']}, «0» у экспертов {k['experts_zero']}, совпало {k['hit']}")
    lines.append("\nХудшие критерии (согласие, n≥5):")
    worst = sorted(((v["ok"] / v["n"], key) for key, v in res["per_key"].items() if v["n"] >= 5))[:15]
    for rate, key in worst:
        v = res["per_key"][key]
        lines.append(f"  {res['number'].get(key)} {key}: {v['ok']}/{v['n']} = {rate:.0%}, пропусков {v['missed']}")
    return "\n".join(lines)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    result = evaluate(*args[:3])
    print(report(result))
    if "--json" in sys.argv:
        target = sys.argv[sys.argv.index("--json") + 1]
        json.dump({k: v for k, v in result.items() if k in ("ids", "pairs", "agree", "zeros_ours", "zeros_ours_hit",
                                                             "zeros_exp", "zeros_exp_found")}, open(target, "w"), ensure_ascii=False)

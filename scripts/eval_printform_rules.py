"""Оценка правил «печатная форма извещения → раздел 1» по эталонным заключениям экспертов.

Для каждой экспертизы берутся печатные формы извещения (``view*.html`` из папки экспертизы, текст — как у
``FileReader.read_html``) и последнее заключение формы «конкурс» из выгрузки. Сравниваются значения 1/0/2.

Запуск::

    python -m scripts.eval_printform_rules "Отчеты и закупки поля.xlsx" expertise_expert_opinion7s.csv expertises/
"""
import glob
import os
import sys
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

from knowledge_store import eis_notice, eis_printform
from scripts.eval_eis_rules import FORM, latest_opinions, norm
from scripts.import_forms import load_forms


def html_to_text(path: str) -> str:
    """Текст HTML так же, как ``FileReader.read_html`` (одна строка — один текстовый узел)."""
    soup = BeautifulSoup(open(path, encoding="utf-8", errors="ignore").read(), "html.parser")
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="\n", strip=True)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def evaluate(xlsx: str, csv_path: str, root: str) -> str:
    """Сравнивает правила печатной формы с эталоном и формирует отчёт по критериям."""
    fields = [{"field_key": f.field_key, "label": f.label, "value_kind": f.value_kind} for f in load_forms(xlsx)[FORM]]
    by_number = {eis_notice.criterion_number(f["label"]): f for f in fields if f["value_kind"] == "presence"}
    opinions = latest_opinions(csv_path)
    stats, misses, used = defaultdict(Counter), defaultdict(list), 0
    for exp_id, data in sorted(opinions.items()):
        findings = {}
        for path in sorted(glob.glob(os.path.join(root, exp_id, "view*.html"))):
            text = html_to_text(path)
            if eis_printform.is_print_form(text):
                findings = eis_printform.evaluate_text(text)
                break
        else:
            continue
        used += 1
        for number, field in by_number.items():
            if not number or number not in eis_printform.RULES:
                continue
            truth = norm(data.get(field["field_key"]))
            if truth is None:
                continue
            finding = findings.get(number)
            if finding is None:
                stats[number]["не решено"] += 1
            elif finding.value == truth:
                stats[number]["верно"] += 1
            else:
                stats[number]["ошибка"] += 1
                misses[number].append(f"{exp_id}: форма={finding.value}, эксперт={truth}")
    lines, total = [f"экспертиз с печатной формой и эталоном: {used}"], Counter()
    for number in sorted(stats, key=lambda n: float(n)):
        c = stats[number]
        total.update(c)
        decided = c["верно"] + c["ошибка"]
        acc = f"{100 * c['верно'] / decided:.0f}%" if decided else "—"
        lines.append(f"{number:5} точность {acc:>5} (верно {c['верно']}, ошибок {c['ошибка']}, не решено {c['не решено']})")
        if c["ошибка"]:
            lines.append("      " + "; ".join(misses[number][:5]))
    decided = total["верно"] + total["ошибка"]
    lines.append(f"ИТОГО: решено {decided} из {sum(total.values())}, точность {100 * total['верно'] / max(decided, 1):.1f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    print(evaluate(*sys.argv[1:4]))

"""Находит тестовых экспертов по косвенным признакам в выгрузке ``users`` (и, если передана, по тексту заключений).

Признаки (роли ``expert`` и ``raoExpert``):

* ``name`` — в имени, почте или организации «тест», «test», «тестовый» и т. п.;
* ``profile`` — у профиля не меньше двух заглушечных реквизитов (ИНН, СНИЛС, паспорт, телефон, БИК, КПП, номер счёта:
  три цифры и меньше либо не более двух различных цифр, например ``11111111111``), либо «Моя работа» / «Моя должность»;
* ``junk-name`` — в имени повторяющиеся буквы (``gggggg``);
* ``opinions`` — в 30% и более заключений (от трёх) встречается слово «тест» (нужен файл заключений).

Скрипт только показывает кандидатов и причины; в обучении участвуют только эксперты из
``configs.settings.PRIMARY_REVIEW_TEST_EXPERT_IDS`` — итоговый список подтверждает человек (признак ``profile`` бывает у
настоящих, но небрежно заполненных учёток). Персональные данные не выводятся: только id, роль, имя и причины.

Запуск::

    python -m scripts.find_test_experts users.csv [expertise_expert_opinion7s.csv]
"""
import csv
import re
import sys
from collections import Counter
from typing import Dict, List, Optional

ROLES = ("expert", "raoExpert")
NAME_RE = re.compile(r"тест|test|демо\b|demo", re.I)
PLACEHOLDER_RE = re.compile(r"моя (работа|должность)|какое-то|наименование организации", re.I)
JUNK_NAME_RE = re.compile(r"(.)\1{5,}")
REQUISITES = ("phone", "snils", "passport", "inn", "number", "bik", "kpp")
TEST_WORD_RE = re.compile(r"тест|test", re.I)


def _digits(value: Optional[str]) -> str:
    """Только цифры значения (``NULL`` и пусто → пустая строка)."""
    return "" if value in (None, "", "NULL") else re.sub(r"\D", "", value)


def is_placeholder(digits: str) -> bool:
    """Заглушка вместо реквизита: до трёх цифр или не более двух различных цифр (``11111111111``)."""
    return bool(digits) and (len(digits) <= 3 or len(set(digits)) <= 2)


def find_candidates(users: List[Dict[str, str]], test_word_share: Optional[Dict[str, float]] = None) -> Dict[str, List[str]]:
    """Кандидаты в тестовые: ``id → причины``.

    Args:
        users: Строки ``users.csv``.
        test_word_share: Необязательно: ``id → доля заключений со словом «тест»`` (для признака ``opinions``).
    """
    found: Dict[str, List[str]] = {}
    for user in users:
        if user.get("role") not in ROLES:
            continue
        reasons = []
        if NAME_RE.search(" ".join(str(user.get(k) or "") for k in ("name", "email", "contactEmail", "organization"))):
            reasons.append("name")
        placeholders = [c for c in REQUISITES if is_placeholder(_digits(user.get(c)))]
        if len(placeholders) >= 2 or PLACEHOLDER_RE.search(" ".join(str(user.get(k) or "") for k in ("organization", "position"))):
            reasons.append("profile")
        if JUNK_NAME_RE.search(user.get("name") or ""):
            reasons.append("junk-name")
        if (test_word_share or {}).get(user["id"], 0) >= 0.3:
            reasons.append("opinions")
        if reasons:
            found[user["id"]] = reasons
    return found


def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: печатает кандидатов с причинами и активностью."""
    csv.field_size_limit(sys.maxsize)
    args = (argv if argv is not None else sys.argv[1:])
    if not args:
        print(__doc__)
        return 1
    with open(args[0], encoding="utf-8") as fh:
        users = list(csv.DictReader(fh))
    share: Dict[str, float] = {}
    counts: Counter = Counter()
    if len(args) > 1:
        import json
        totals: Counter = Counter()
        tests: Counter = Counter()
        with open(args[1], encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                totals[row["expert_id"]] += 1
                try:
                    data = json.loads(row["data"])
                except ValueError:
                    continue
                if TEST_WORD_RE.search(" ".join(v for v in data.values() if isinstance(v, str))):
                    tests[row["expert_id"]] += 1
        counts = totals
        share = {e: tests[e] / n for e, n in totals.items() if n >= 3}
    by_id = {u["id"]: u for u in users}
    for uid, reasons in sorted(find_candidates(users, share).items(), key=lambda x: int(x[0])):
        print(f"{uid:>5} {by_id[uid]['role']:10} {by_id[uid]['name'][:34]:34} заключений {counts[uid]:3}  {', '.join(reasons)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

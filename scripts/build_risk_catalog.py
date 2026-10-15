"""Сборка ``risk_monitoring/config/risk_catalog.json`` из таблиц ``docs/risk_catalog.md``.

Источник истины каталога рисков — документ ``docs/risk_catalog.md`` (его же согласуют методологи
и разработчики внешней системы). Скрипт разбирает строки таблиц вида
``| Код | Название | Вид | Стадия | Показатель | Где выявляется | 44-ФЗ | 223-ФЗ | Код концепции |``
и пишет JSON, который читает ``risk_monitoring/risk_catalog.py`` (и может читать PHP).

Запуск: ``python -m scripts.build_risk_catalog`` (проверка без записи: ``--check``).
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "risk_catalog.md"
TARGET = ROOT / "risk_monitoring" / "config" / "risk_catalog.json"

ROW_RE = re.compile(r"^\|\s*([A-Z]+-\d{3})\s*\|")
INDICATOR_RE = re.compile(r"^(?P<code>[a-z]{2}_[pn]\d+)\s*\(№\s*(?P<no>\d+)\s*(?P<status>[✅⏸])\s*\)$")
KINDS = {"риск": "risk", "нарушение": "violation", "ИИ": "ai"}
EMPTY = {"—", "-", ""}
NOT_APPLICABLE = "не применяется"


def _law(text: str) -> Optional[str]:
    """Норма из ячейки: «—» и «не применяется» → ``None``, «Положение» → «положение о закупке заказчика».

    Args:
        text: Текст ячейки.

    Returns:
        Optional[str]: Норма или ``None``.
    """
    text = text.strip()
    if text in EMPTY or text == NOT_APPLICABLE:
        return None
    return re.sub(r"(?<![а-яё])Положение(?![а-яё])", "положение о закупке заказчика", text)


def parse_row(line: str) -> Dict[str, Any]:
    """Разбирает одну строку таблицы каталога.

    Args:
        line: Строка markdown-таблицы.

    Returns:
        Dict[str, Any]: Запись каталога.

    Raises:
        ValueError: Неверное число колонок, неизвестный вид или формат показателя.
    """
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) != 9:
        raise ValueError(f"ожидалось 9 колонок, получено {len(cells)}: {line[:80]}")
    code, title, kind, stage, indicator, detected_by, law_44, law_223, concept = cells
    if kind not in KINDS:
        raise ValueError(f"{code}: неизвестный вид «{kind}»")
    ind: Dict[str, Any] = {"indicator": None, "rule_no": None, "rule_active": None}
    if indicator not in EMPTY:
        m = INDICATOR_RE.match(indicator)
        if not m:
            raise ValueError(f"{code}: не разобран показатель «{indicator}»")
        ind = {"indicator": m["code"], "rule_no": int(m["no"]), "rule_active": m["status"] == "✅"}
    return {
        "code": code,
        "category": code.split("-")[0],
        "title": title,
        "kind": KINDS[kind],
        "stage": None if stage in EMPTY else stage,
        **ind,
        "detected_by": detected_by,
        "law_44": _law(law_44),
        "law_223": _law(law_223),
        "applicable_223": law_223.strip() != NOT_APPLICABLE,
        "concept_code": None if concept in EMPTY else concept,
    }


def build(source: Path = SOURCE) -> Dict[str, Any]:
    """Собирает каталог из документа.

    Args:
        source: Путь к ``docs/risk_catalog.md``.

    Returns:
        Dict[str, Any]: ``{"version", "risks": [...]}``.

    Raises:
        ValueError: Повтор кода или показателя.
    """
    text = source.read_text(encoding="utf-8")
    version = re.search(r"`(rm-catalog-[\w.-]+)`", text)
    risks: List[Dict[str, Any]] = [parse_row(line) for line in text.splitlines() if ROW_RE.match(line)]
    codes = [r["code"] for r in risks]
    dup = {c for c in codes if codes.count(c) > 1}
    if dup:
        raise ValueError(f"повторяются коды: {sorted(dup)}")
    inds = [r["indicator"] for r in risks if r["indicator"]]
    dup = {c for c in inds if inds.count(c) > 1}
    if dup:
        raise ValueError(f"показатель привязан к нескольким кодам: {sorted(dup)}")
    return {"version": version.group(1) if version else "rm-catalog", "source": "docs/risk_catalog.md", "risks": risks}


def main() -> int:
    """Точка входа: пишет JSON или проверяет, что он актуален (``--check``)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="только проверить, что JSON совпадает с документом")
    args = parser.parse_args()
    data = json.dumps(build(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        ok = TARGET.exists() and TARGET.read_text(encoding="utf-8") == data
        print("актуален" if ok else "устарел: запустите python -m scripts.build_risk_catalog")
        return 0 if ok else 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(data, encoding="utf-8")
    print(f"записано {TARGET.relative_to(ROOT)}: {len(json.loads(data)['risks'])} рисков")
    return 0


if __name__ == "__main__":
    sys.exit(main())

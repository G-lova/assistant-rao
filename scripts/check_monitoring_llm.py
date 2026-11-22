"""Прогон ИИ-слоя мониторинга закупок на реальной модели и подсчёт доли цитат, прошедших проверку.

Для каждой папки закупки читает документы (как ``scripts/check_monitoring_nmck.py``: PDF с распознаванием
сканов, DOCX, TXT), отправляет их модели по критериям ``NMCK`` и ``OBJECT``
(:mod:`knowledge_store.monitoring_llm_review`) и печатает найденные замечания с цитатами. Показатели:

* ``raw`` — замечания, которые вернула модель;
* ``verified`` — замечания, цитата которых дословно есть в документах (остальные отброшены);
* ``quote_pass_rate`` — ``verified / raw``: чем ближе к 1, тем реже модель выдумывает цитаты.

Рядом выводится вердикт правил по тому же критерию (для НМЦК), чтобы сравнить два слоя.

Запуск на dev/stage (нужен доступ к llmhub, как у остальных задач сервиса)::

    python -m scripts.check_monitoring_llm --out llm_monitoring.json "/путь/САО ЭА-02" "/путь/Псков" "/путь/Мурманск"
"""
import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from knowledge_store import monitoring_llm_review as mlr
from knowledge_store import monitoring_nmck as nm
from scripts.check_monitoring_nmck import docx_text, pdf_text, read_purchase

_OBJECT_NAME = r"договор|контракт|\bтз\b|техническ|задани"   # документы, по которым проверяется описание объекта


def purchase_docs(folder: Path, cache_dir: Path) -> Dict[str, str]:
    """Все документы папки закупки: имя файла → текст.

    В отличие от ``read_purchase`` договоры и контракты не пропускаются: по ним ИИ-слой проверяет описание объекта.
    """
    docs: Dict[str, str] = {}
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".txt")):
        if f.suffix.lower() == ".pdf":
            text = pdf_text(f, cache_dir)
        elif f.suffix.lower() == ".docx":
            text = docx_text(f)
        else:
            text = f.read_text(encoding="utf-8", errors="replace")
        if text and text.strip():
            docs[f.name] = text
    return docs


def select_docs(docs: Dict[str, str], criterion: str) -> Dict[str, str]:
    """Документы для критерия: НМЦК — без договоров/актов (как у правил), объект — договор/ТЗ первыми, если они есть."""
    if criterion == "NMCK":
        return {n: t for n, t in docs.items() if not nm.should_skip_by_name(Path(n).stem)} or docs
    first = {n: t for n, t in docs.items() if re.search(_OBJECT_NAME, n, re.I)}
    return {**first, **{n: t for n, t in docs.items() if n not in first}}


async def run(folders: List[str], llm, cache_dir: Path, debug: bool = False) -> Dict:
    """Прогоняет модель по папкам и считает сводные показатели.

    Args:
        folders: Папки закупок.
        llm: ``LLMJson`` (или объект с ``ask``).
        cache_dir: Каталог кэша распознанного текста.
        debug: Печатать ответ модели до проверки цитат.

    Returns:
        dict: ``purchases`` (по папкам и критериям) и ``summary`` (raw / verified / quote_pass_rate).
    """
    result: Dict = {"purchases": {}, "summary": {}}
    totals = {c: {"raw": 0, "verified": 0} for c in mlr.CRITERIA}
    for folder in folders:
        docs = purchase_docs(Path(folder), cache_dir)
        entry = {"files": list(docs), "rules_nmck": nm.check_purchase(read_purchase(Path(folder), cache_dir))["verdict"]}
        for criterion in mlr.CRITERIA:
            trace: Dict = {}
            try:
                await mlr.review(llm, criterion, select_docs(docs, criterion), trace)
            except Exception as exc:        # сбой модели по одной закупке не должен останавливать прогон
                trace["error"] = f"{type(exc).__name__}: {exc}"
            entry[criterion] = trace
            totals[criterion]["raw"] += len(trace.get("raw") or [])
            totals[criterion]["verified"] += len(trace.get("verified") or [])
            if debug:
                print(json.dumps(trace.get("raw"), ensure_ascii=False)[:800])
            print(f"{folder} [{criterion}] модель: {len(trace.get('raw') or [])}, с верной цитатой: "
                  f"{len(trace.get('verified') or [])}; правила НМЦК: {entry['rules_nmck']}")
            for f in trace.get("verified") or []:
                print(f"   {f['document']}: «{f['quote'][:120]}» — {f['explanation']}")
        result["purchases"][folder] = entry
    for criterion, t in totals.items():
        t["quote_pass_rate"] = round(t["verified"] / t["raw"], 3) if t["raw"] else None
    result["summary"] = totals
    return result


async def main(argv=None) -> int:
    """Точка входа: прогон, сводка, JSON с подробностями."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folders", nargs="+", help="Папки закупок")
    parser.add_argument("--out", default="llm_monitoring.json")
    parser.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "nmck_ocr_cache"))
    parser.add_argument("--debug", action="store_true", help="печатать ответ модели до проверки цитат")
    args = parser.parse_args(argv)

    from configs.llm_client import get_llm
    from risk_monitoring.llm_json import LLMJson

    client, model = get_llm()
    llm = LLMJson(client, model, rate=1.0, concurrency=2)
    result = await run(args.folders, llm, Path(args.cache), args.debug)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

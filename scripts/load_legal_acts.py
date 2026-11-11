"""Загрузка редакций нормативных актов из файлов (PDF / HTML / TXT сводных редакций КонсультантПлюс).

Запуск::

    python -m scripts.load_legal_acts --edition-date 2026-08-04 "44-ФЗ.pdf"
    python -m scripts.load_legal_acts --dry-run *.pdf        # только разбор: акт, дата редакции, число норм

Дата редакции берётся из ``--edition-date``, из имени файла («ред. от 04.08.2026») или — с предупреждением — из
списка изменяющих документов / даты принятия. Эмбеддинги — если доступен сервис эмбеддингов (иначе нормы
сохраняются без них, проверка ссылок всё равно работает).
"""
import argparse
import asyncio
import json
import os


async def main(paths, edition_date, dry_run):
    """Разбирает и сохраняет каждый файл; печатает результат JSON-строкой на файл."""
    from risk_monitoring import legal_acts

    if dry_run:
        for p in paths:
            try:
                act = legal_acts.parse_act(legal_acts.read_source(p), os.path.basename(p), edition_date)
            except ValueError as e:
                print(json.dumps({"file": p, "error": str(e)}, ensure_ascii=False))
                continue
            print(json.dumps({"file": p, **{k: act.get(k) for k in ("act_key", "kind", "title", "edition_date",
                                                                    "edition_source")},
                              "norms": len(act["norms"])}, ensure_ascii=False))
        return
    from configs.ai_repo import AIRepo
    from configs.http_client_manager import HTTPClientManager

    async with HTTPClientManager(timeout=180.0) as mgr, AIRepo() as repo:
        loader = legal_acts.LegalActsLoader(repo, mgr)
        for p in paths:
            try:
                res = await loader.load_text(legal_acts.read_source(p), os.path.basename(p), edition_date)
            except ValueError as e:
                res = {"error": str(e)}
            print(json.dumps({"file": p, **res}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--edition-date", help="дата редакции YYYY-MM-DD (одна на все файлы)")
    ap.add_argument("--dry-run", action="store_true", help="только разбор, без записи в БД")
    a = ap.parse_args()
    asyncio.run(main(a.paths, a.edition_date, a.dry_run))

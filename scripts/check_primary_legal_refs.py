"""Проверяет ссылки на нормы закона в замечаниях первичных экспертов (кандидат в категорию «Нормативные ошибки»).

Как это работает: из текста замечаний (``field3`` и текстовые поля критериев ``*_text``) берутся фрагменты со ссылками на
нормы (``parse_refs`` из :mod:`risk_monitoring.legal_refs`), каждая проверяется по загруженным в сервис редакциям актов
(таблицы ``rm_legal_acts`` / ``rm_legal_norms``): ``found`` — норма есть, ``lost_force`` — утратила силу в редакции на дату
заключения, ``not_found`` — такой статьи/части/пункта в акте нет, ``unknown_act`` — акт не загружен или не определён.
Ошибкой первичного считается ``not_found`` (и ``lost_force``): так же проверяются нормы в рисках мониторинга.

Закон закупки (44-ФЗ / 223-ФЗ) нужен для ссылок без названия акта; берётся из ``pe_procurements.law`` (``--procurements``);
для отчётных документов без закона такие ссылки получают статус ``unknown_act``.

Скрипт запускается на dev/stage (нужна БД сервиса с загруженными актами), результат — CSV и сводка статусов, в том числе
связь статуса с оценкой сводного эксперта (``--experts``), чтобы решить, подключать ли категорию в ответ эндпоинта.

Запуск::

    python -m scripts.check_primary_legal_refs expertise_expert_opinion7s.csv --procurements pe_procurements.csv \\
        --experts expertise_experts.csv --users users.csv --out legal_refs_check.csv
"""
import argparse
import asyncio
import csv
import json
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

from risk_monitoring import legal_refs

LAW_ACTS = {"44-ФЗ": "44-ФЗ", "223-ФЗ": "223-ФЗ"}      # значение pe_procurements.law → ключ акта по умолчанию
BAD_STATUSES = ("not_found", "lost_force")


def remark_texts(data: Dict[str, Any]) -> List[str]:
    """Тексты, в которых эксперт может ссылаться на норму: замечания ``field3`` и текстовые поля критериев ``*_text``."""
    texts = [data.get("field3")] + [v for k, v in data.items() if k.endswith("_text")]
    return [t for t in texts if isinstance(t, str) and t.strip()]


def extract_refs(data: Dict[str, Any], default_act: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ссылки на нормы из замечаний заключения (``parse_refs`` по каждому тексту).

    Args:
        data: Содержимое заключения (``expertise_expert_opinions.data``).
        default_act: Акт по умолчанию для ссылок без названия закона.

    Returns:
        list[dict]: Ссылки в формате :func:`risk_monitoring.legal_refs.parse_refs`.
    """
    return [ref for text in remark_texts(data) for ref in legal_refs.parse_refs(text, default_act)]


def load_laws(path: Optional[str]) -> Dict[str, Optional[str]]:
    """``expertise_id → ключ акта по умолчанию`` из выгрузки ``pe_procurements`` (пусто без файла)."""
    if not path:
        return {}
    csv.field_size_limit(sys.maxsize)
    with open(path, encoding="utf-8") as fh:
        return {r["expertise_id"]: LAW_ACTS.get(r["law"]) for r in csv.DictReader(fh)}


def load_expert_rows(path: Optional[str]) -> Dict[tuple, dict]:
    """``(expertise_id, expert_id) → строка expertise_experts`` (оценка ``range``, комментарий)."""
    if not path:
        return {}
    csv.field_size_limit(sys.maxsize)
    with open(path, encoding="utf-8") as fh:
        return {(r["expertise_id"], r["expert_id"]): r for r in csv.DictReader(fh)}


def summarize(rows: Iterable[Dict[str, Any]], experts: Dict[tuple, dict]) -> Dict[str, Any]:
    """Сводка проверки: ссылки по статусам; число заключений с плохими ссылками; средняя оценка с ними и без них.

    Args:
        rows: Строки результата (``expertise_id``, ``expert_id``, ``status`` …).
        experts: Строки ``expertise_experts`` (:func:`load_expert_rows`) — для сопоставления с оценкой.

    Returns:
        dict: ``refs`` (счётчик статусов), ``opinions`` (заключений со ссылками), ``bad_opinions`` (с ``not_found`` /
        ``lost_force``), ``mean_range_bad`` и ``mean_range_ok`` (средний ``range``; ``None`` без оценок).
    """
    status = Counter()
    per_opinion: Dict[tuple, set] = defaultdict(set)
    for r in rows:
        status[r["status"]] += 1
        per_opinion[(r["expertise_id"], r["expert_id"])].add(r["status"])
    bad = {k for k, s in per_opinion.items() if s & set(BAD_STATUSES)}

    def mean(keys: Iterable[tuple]) -> Optional[float]:
        """Средняя оценка по заключениям с известным ``range``."""
        vals = [float(experts[k]["range"]) for k in keys if k in experts and experts[k]["range"] not in ("NULL", "")]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {"refs": dict(status), "opinions": len(per_opinion), "bad_opinions": len(bad),
            "mean_range_bad": mean(bad), "mean_range_ok": mean(set(per_opinion) - bad)}


async def check_all(conn: Any, opinions_csv: str, laws: Dict[str, Optional[str]],
                    primary_ids: Optional[set]) -> List[Dict[str, Any]]:
    """Проверяет ссылки всех заключений выгрузки.

    Args:
        conn: Соединение/пул Postgres сервиса (``LawChecker`` только читает).
        opinions_csv: Выгрузка ``expertise_expert_opinions``.
        laws: ``expertise_id → акт по умолчанию`` (:func:`load_laws`).
        primary_ids: Идентификаторы первичных экспертов (``users.role = 'expert'``); ``None`` — все.

    Returns:
        list[dict]: По строке на ссылку: ``expertise_id``, ``expert_id``, ``ref``, ``act``, ``status``, ``reason``,
        ``address``.
    """
    checker = legal_refs.LawChecker(conn)
    csv.field_size_limit(sys.maxsize)
    out: List[Dict[str, Any]] = []
    with open(opinions_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if primary_ids is not None and row["expert_id"] not in primary_ids:
                continue
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            on = (row.get("updated_at") or "")[:10] or None          # редакция акта на дату заключения
            default_act = laws.get(row["expertise_id"])
            for text in remark_texts(data):
                result = await checker.check(text, default_act, on)
                for ref in result["refs"]:
                    out.append({"expertise_id": row["expertise_id"], "expert_id": row["expert_id"],
                                "ref": ref["ref"], "act": ref.get("act"), "status": ref["status"],
                                "reason": ref.get("reason", ""), "address": ref.get("address", "")})
    return out


def load_primary_ids(users_csv: Optional[str]) -> Optional[set]:
    """Идентификаторы пользователей с ролью ``expert``; ``None`` без файла."""
    if not users_csv:
        return None
    csv.field_size_limit(sys.maxsize)
    with open(users_csv, encoding="utf-8") as fh:
        return {r["id"] for r in csv.DictReader(fh) if r.get("role") == "expert"}


async def main(argv: Optional[List[str]] = None) -> int:
    """Точка входа: проверка ссылок, запись CSV, сводка."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("opinions_csv")
    parser.add_argument("--procurements", help="pe_procurements.csv (закон закупки для ссылок без названия акта)")
    parser.add_argument("--experts", help="expertise_experts.csv (оценки — для связи статуса с оценкой)")
    parser.add_argument("--users", help="users.csv (берутся только первичные эксперты)")
    parser.add_argument("--out", default="legal_refs_check.csv")
    args = parser.parse_args(argv)

    from configs.working_with_db import get_async_db_connection

    async with get_async_db_connection() as conn:
        rows = await check_all(conn, args.opinions_csv, load_laws(args.procurements), load_primary_ids(args.users))
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["expertise_id", "expert_id", "ref", "act", "status", "reason", "address"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summarize(rows, load_expert_rows(args.experts)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

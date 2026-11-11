"""Положения о закупке организаций за год (clause_coverage) и дозагрузка из ЕИС за период (clause_backfill)."""
import asyncio
import os
import tempfile
import unittest
from datetime import date

from risk_monitoring import clause_backfill as cb
from risk_monitoring import clause_coverage as cc


def v(reg, ver, base, ctype="typal", inn="1", head=None):
    """Версия положения с заказчиком."""
    return {"registration_number": reg, "version": ver, "use_for_base_date": base, "clause_type": ctype,
            "head_registration_number": head, "name": f"Положение {reg}", "creator_name": "Минобрнауки",
            "head_name": "АО Голова" if head else None, "inn": inn}


class ActiveTest(unittest.TestCase):
    """Версия действует от своей даты до даты следующей версии."""

    def test_active_in_year(self):
        """Версия 2025 года, заменённая в марте 2026, — действовала в 2026; заменённая в 2025 — нет."""
        allv = {"A": [v("A", 1, "2024-01-10"), v("A", 2, "2025-05-01"), v("A", 3, "2026-03-01")]}
        act = cc.active_in_year([v("A", 2, "2025-05-01")], allv, 2026)
        self.assertEqual((act[0]["active_from"], act[0]["active_to"]), ("2025-05-01", "2026-02-28"))
        self.assertEqual(cc.active_in_year([v("A", 1, "2024-01-10")], allv, 2026), [])
        self.assertEqual(cc.active_in_year([v("A", 3, "2026-03-01")], allv, 2026)[0]["active_to"], None)
        # версия, вступающая в силу в 2027, в 2026 не действовала
        self.assertEqual(cc.active_in_year([v("B", 1, "2027-01-01")], {"B": [v("B", 1, "2027-01-01")]}, 2026), [])


class ClassifyTest(unittest.TestCase):
    """Статус организации."""

    ORG = {"id": 5, "inn": "1", "name": "Университет"}

    def test_statuses(self):
        """Типовое, собственное (+ пометка при типовом), присоединение, нет положения с закупками 223 и без."""
        typal = {**v("A", 3, "2026-03-01"), "active_from": "2026-03-01", "active_to": None}
        own = {**v("C", 1, "2026-02-01", "customer"), "active_from": "2026-02-01", "active_to": None}
        joined = {**v("D", 1, "2026-02-01", "customer", head="H"), "active_from": "2026-02-01", "active_to": None}
        r = cc.classify(self.ORG, [typal], 0, 2026)
        self.assertEqual((r["status"], r["clause_number"], r["clause_owner"]), ("typal", "A", "Минобрнауки"))
        r = cc.classify(self.ORG, [typal, own], 0, 2026)
        self.assertEqual(r["status"], "own")
        self.assertIn("собственное положение при типовом", r["note"])
        r = cc.classify(self.ORG, [joined], 0, 2026)
        self.assertEqual((r["status"], r["clause_owner"]), ("joined", "АО Голова"))
        self.assertEqual(cc.classify(self.ORG, [], 3, 2026)["status"], "none_with_223")
        self.assertEqual(cc.classify(self.ORG, [], 0, 2026)["status_name"], "Нет положения")

    def test_left_typal(self):
        """Выведена из типового в течение года — пометка."""
        typal = {**v("A", 2, "2025-05-01"), "active_from": "2025-05-01", "active_to": "2026-02-28"}
        self.assertIn("выведена", cc.classify(self.ORG, [typal], 0, 2026)["note"])


class FakePool:
    """Пул: версии с заказчиками, все версии, журнал; запись журнала."""

    def __init__(self, rows, allv, done=()):
        """Строки для запросов отчёта и выполненные пары журнала."""
        self.rows, self.allv, self.done, self.log = rows, allv, list(done), []
        self.covered = set()

    async def fetch(self, sql, *args):
        """Ответ по виду запроса."""
        if "SELECT DISTINCT cu.inn" in sql:
            return [{"inn": i} for i in args[0] if i in self.covered]
        if "FROM rm_typal_clause_customers cu JOIN" in sql:
            return [r for r in self.rows if r["inn"] in args[0]]
        if "FROM rm_typal_clauses\nWHERE registration_number" in sql:
            return [r for r in self.allv if r["registration_number"] in args[0]]
        if "status IN ('done', 'no_data')" in sql and "WHERE kind" in sql:
            return [{"region": r, "day": date.fromisoformat(d)} for r, d in self.done]
        if "rm_eis_backfill_log WHERE day BETWEEN" in sql:
            return [{"kind": "typal", "loaded": 10, "errors": 0, "first_day": date(2026, 1, 1),
                     "last_day": date(2026, 11, 10)}]
        return []

    async def execute(self, sql, *args):
        """Запись журнала."""
        self.log.append(args)


class BuildTest(unittest.TestCase):
    """Сборка отчёта: одна строка на ИНН, закупки 223 по всем записям ИНН, таблица."""

    def test_build_and_xlsx(self):
        """Две записи справочника с одним ИНН — одна строка; закупки суммируются; xlsx пишется."""
        orgs = [{"id": 1, "inn": "1", "name": "Вуз", "is_actual": "0", "monitoring": "0"},
                {"id": 2, "inn": "1", "name": "Вуз (акт.)", "is_actual": "1", "monitoring": "1"},
                {"id": 3, "inn": "2", "name": "Школа"}]
        pool = FakePool([v("A", 3, "2026-03-01", inn="1")], [v("A", 3, "2026-03-01")])
        rep = asyncio.run(cc.build(pool, orgs, {1: 2, 3: 4}, 2026))
        by = {r["inn"]: r for r in rep["rows"]}
        self.assertEqual((by["1"]["name"], by["1"]["status"], by["1"]["purchases_223"]), ("Вуз (акт.)", "typal", 2))
        self.assertEqual(by["2"]["status"], "none_with_223")
        self.assertEqual(rep["totals"], {"typal": 1, "none_with_223": 1})
        with tempfile.TemporaryDirectory() as d:
            path = cc.to_xlsx(rep, os.path.join(d, "r.xlsx"))
            from openpyxl import load_workbook

            ws = load_workbook(path).active
            self.assertEqual(ws.cell(2, 1).value, "Школа")       # сначала без положения
            self.assertEqual(ws.cell(1, 14).value, "Решение (заполняет юрист)")


class FakeLoader:
    """Загрузчик: регион 77 за 10.11 — версия, остальное — нет данных; 50 за 09.11 — сбой."""

    def __init__(self, pool):
        """Репозиторий с пулом, счётчики."""
        self.repo = type("R", (), {"pool": pool})()
        self.stats = {}
        self.calls = []

    async def load_region(self, reg, day, kind="typal"):
        """Ответ ЕИС по региону и дню."""
        self.calls.append((kind, reg, day))
        if (reg, day) == ("50", "2026-11-09"):
            self.last_eis = {"status": "error", "error": "Код 13"}
            return []
        if (reg, day) == ("77", "2026-11-10"):
            self.last_eis = {"status": "success"}
            return [{"registration_number": "A"}]
        self.last_eis = {"status": "no_data"}
        return []


class BackfillTest(unittest.TestCase):
    """Порядок регионов и дней, журнал, пропуск выполненного, лимит запросов."""

    def test_region_order(self):
        """Москва первой, затем по числу организаций."""
        self.assertEqual(cb.region_order(["5001", "5002", "3401", "7701"]), ["77", "50", "34"])
        self.assertEqual(cb.region_order(["3401"])[0], "77")

    def test_run(self):
        """Дни назад; выполненная пара пропущена; ошибка в журнале; положения заказчиков не читаются, но хранятся."""
        pool = FakePool([], [], done=[("77", "2026-11-09")])
        loader = FakeLoader(pool)
        bf = cb.ClauseBackfill(loader, ["7701", "5001"])
        self.assertTrue(loader.keep_others_metadata)
        self.assertFalse(loader.read_files)
        res = asyncio.run(bf.run(date(2026, 11, 9), date(2026, 11, 10), kinds=["typal"]))
        self.assertEqual(loader.calls, [("typal", "77", "2026-11-10"), ("typal", "50", "2026-11-10"),
                                        ("typal", "50", "2026-11-09")])
        self.assertEqual((res["requests"], res["versions"], res["errors"], res["skipped"]), (3, 1, 1, 1))
        statuses = [a[3] for a in pool.log]
        self.assertEqual(statuses, ["done", "no_data", "error"])

    def test_customer_scope(self):
        """Положения заказчиков — только по регионам организаций без типового положения."""
        pool = FakePool([], [])
        pool.covered = {"7701"}
        loader = FakeLoader(pool)
        res = asyncio.run(cb.ClauseBackfill(loader, ["7701", "5001"]).run(date(2026, 11, 10), date(2026, 11, 10),
                                                                          kinds=["customer"]))
        self.assertEqual(res["regions"]["customer"], ["50"])
        self.assertEqual(loader.calls, [("customer", "50", "2026-11-10")])

    def test_max_requests(self):
        """Порция: после max_requests остальное — в left."""
        loader = FakeLoader(FakePool([], []))
        res = asyncio.run(cb.ClauseBackfill(loader, ["7701"]).run(date(2026, 11, 1), date(2026, 11, 10),
                                                                  kinds=["typal"], max_requests=4))
        self.assertEqual((res["requests"], res["left"]), (4, 6))


if __name__ == "__main__":
    unittest.main()

"""Дозагрузка организации (25.11.2026): ``organisations/analysis`` с ``analyze_contracts`` — закупки организации за
период порциями, затем аналитика организации; порции ждут незавершённый ежедневный прогон."""
import asyncio
import types
import unittest
from unittest import mock

from configs import settings


class ContractIdsTest(unittest.TestCase):
    """OrgAnalyzer.contract_ids: закупки как заказчика (все строки группы) и как поставщика (по ИНН), по ролям."""

    def _oa(self, roles):
        """Анализатор с подменой запросов к внешней БД."""
        from tests.stubs import stubbed_import

        ctx = stubbed_import("risk_monitoring.org_analyzer")
        m = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        oa = m.OrgAnalyzer.__new__(m.OrgAnalyzer)
        oa.roles, oa.period_months = roles, 12
        asked = []

        async def query(name, head=(), **lists):
            """Ответы внешней БД."""
            asked.append((name, head, lists))
            if name == "org_rows.sql":
                return [{"id": 5, "inn": "7707083893", "kpp": "770701001", "year": 2026}]
            if name == "org_group.sql":
                return [{"id": 5, "inn": "7707083893", "kpp": "770701001", "year": 2026},
                        {"id": 6, "inn": "7707083893", "kpp": "770701001", "year": 2025}]
            if name == "customer_contracts.sql":
                return [{"id": 12, "date_public": "2026-03-01"}, {"id": 11, "date_public": "2026-01-10"}]
            if name == "supplier_contracts.sql":
                return [{"id": 12, "date_public": "2026-03-01"}, {"id": 30, "date_public": "2026-02-01"}]
            return []

        oa._query = query
        return oa, asked

    def test_both_roles(self):
        """Обе роли: без повторов, старые первыми; заказчик — по всем строкам группы."""
        oa, asked = self._oa(("customer", "supplier"))
        ids = asyncio.run(oa.contract_ids([5]))
        self.assertEqual(ids, [11, 30, 12])
        cust = next(a for a in asked if a[0] == "customer_contracts.sql")
        self.assertEqual(cust[2]["ids"], [5, 6])

    def test_customer_only(self):
        """Только заказчик — запроса поставщика нет."""
        oa, asked = self._oa(("customer",))
        self.assertEqual(asyncio.run(oa.contract_ids([5])), [11, 12])
        self.assertFalse(any(a[0] == "supplier_contracts.sql" for a in asked))


class ScheduleTest(unittest.TestCase):
    """Цепочка: порции закупок, последней — аналитика организаций без analyze_contracts."""

    def test_chain(self):
        """65 закупок по 30 — 3 порции + аналитика."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.tasks") as t:
            built = {}

            def fake_chain(*steps):
                """Цепочка: запоминает шаги, возвращает результат с родителями."""
                built["steps"] = steps
                last = types.SimpleNamespace(id="org", parent=None)
                node = last
                for n in range(len(steps) - 1, 0, -1):
                    node.parent = types.SimpleNamespace(id=f"p{n}", parent=None)
                    node = node.parent
                return types.SimpleNamespace(apply_async=lambda: last)

            celery_mod = types.SimpleNamespace(chain=fake_chain)

            class OA:
                """OrgAnalyzer: 65 карточек."""

                def __init__(self, *a, **kw):
                    """Параметры."""

                async def contract_ids(self, ids):
                    """Карточки."""
                    return list(range(1, 66))

            class Mgr:
                """HTTP-менеджер."""

                def __init__(self, **kw):
                    """Параметры."""

                async def __aenter__(self):
                    """Открытие."""
                    return self

                async def __aexit__(self, *a):
                    """Закрытие."""

            with mock.patch.dict("sys.modules", {"celery": celery_mod,
                                                 "risk_monitoring.org_analyzer": types.SimpleNamespace(OrgAnalyzer=OA),
                                                 "configs.http_client_manager":
                                                     types.SimpleNamespace(HTTPClientManager=Mgr)}), \
                    mock.patch.object(t, "rm_org_contracts_step_task") as step_task, \
                    mock.patch.object(t, "rm_analyze_orgs_task") as org_task, \
                    mock.patch.object(settings, "ORG_CONTRACTS_PORTION", 30):
                out = t._schedule_org_contracts([5], "prod", ["customer"], None, True, True, False)
            self.assertEqual((out["status"], out["contracts"], out["portions"]), ("scheduled", 65, 3))
            step_si, org_si = step_task.si, org_task.si
            self.assertEqual([c.args[0] for c in step_si.call_args_list], [list(range(1, 31)), list(range(31, 61)),
                                                                           list(range(61, 66))])
            self.assertFalse(org_si.call_args.args[-1])          # аналитика — без analyze_contracts
            self.assertEqual(len(built["steps"]), 4)
            self.assertEqual(out["tasks"], ["p1", "p2", "p3", "org"])
            self.assertEqual(out["organisations_task"], "org")


class StepTest(unittest.TestCase):
    """Порция: ждёт незавершённый прогон; ошибка не обрывает цепочку."""

    def test_busy_and_error(self):
        """Прогон не доделан — busy; иначе анализ закупок; исключение — error в итоге."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.tasks") as t:
            async def busy(env):
                """Прогон за 24.11 не доделан."""
                return "2026-11-24"

            async def free(env):
                """Прогон доделан."""
                return None

            with mock.patch.object(settings, "ORG_BACKFILL_YIELD_TO_DAILY", True), \
                    mock.patch.object(t, "_daily_busy", busy):
                out = t._org_contracts_step([1], "prod", True, True, 1, 2, "n", "id")
            self.assertEqual(out["busy"], "2026-11-24")
            self.assertGreater(out["wait"], 0)

            with mock.patch.object(settings, "ORG_BACKFILL_YIELD_TO_DAILY", True), \
                    mock.patch.object(t, "_daily_busy", free), \
                    mock.patch.object(t, "_analyze_contracts", return_value={"summary": {"done": 1}}) as ac:
                out = t._org_contracts_step([1, 2], "prod", True, True, 1, 2, "n", "id")
            self.assertEqual(out["summary"], {"done": 1})
            self.assertTrue(ac.call_args.args[6])                 # analyze_documents

            with mock.patch.object(settings, "ORG_BACKFILL_YIELD_TO_DAILY", False), \
                    mock.patch.object(t, "_analyze_contracts", side_effect=TimeoutError("предел")):
                out = t._org_contracts_step([1], "prod", True, True, 2, 2, "n", "id")
            self.assertIn("TimeoutError", out["error"])


class RouterTest(unittest.TestCase):
    """Параметр запроса — по умолчанию выключен (существующий эндпоинт не меняется)."""

    def test_default(self):
        """analyze_contracts = False по умолчанию."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.router") as r:
            req = r.PackageRequest(ids=[5])
            self.assertFalse(req.analyze_contracts)
            self.assertTrue(r.PackageRequest(ids=[5], analyze_contracts=True).analyze_contracts)


if __name__ == "__main__":
    unittest.main()

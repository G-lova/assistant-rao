"""Изменения по ТЗ внешней БД от 09.10.2026: source риска, РНП (REG-002), профиль организации, сводки прогонов."""
import asyncio
import unittest
from datetime import date
from unittest import mock

from configs import settings
from risk_monitoring import contract_package as cp
from risk_monitoring import org_profile, passport


class SourceTest(unittest.TestCase):
    """Колонка ``source`` в ``risk_monitoring_document_risks``: один главный источник на риск."""

    def test_primary_source(self):
        """Реестр важнее правила, правило — модели; print_form → rule, version_diff → ai_file."""
        self.assertEqual(passport.primary_source(["ai_file", "rule"]), "rule")
        self.assertEqual(passport.primary_source(["ai_event", "registry", "rule"]), "registry")
        self.assertEqual(passport.primary_source(["print_form"]), "rule")
        self.assertEqual(passport.primary_source(["version_diff"]), "ai_file")
        self.assertIsNone(passport.primary_source([]))

    def test_merge_sets_source(self):
        """Объединённый риск получает и ``sources``, и ``source``."""
        out = passport.merge_risks([{"rule_id": "FIN-004", "level": 0.4, "confidence": 1, "source": "ai_file"}],
                                   [{"rule_id": "FIN-004", "level": 0.4, "confidence": 1, "source": "rule"}])
        self.assertEqual(out[0]["sources"], ["ai_file", "rule"])
        self.assertEqual(out[0]["source"], "rule")


class RnpTest(unittest.TestCase):
    """РНП (``risk_monitoring_rnp_entries``) → REG-002."""

    entry = {"id": 7, "fz": 44, "reg_number": "РНП.123", "status": "active", "has_inclusion": 1, "inn": "1660331280",
             "kpp": None, "published_at": "2026-03-01 10:00:00.000", "first_published_at": "2026-03-01 10:00:00.000",
             "planned_exclusion_date": "2028-03-01", "excluded_at": None}

    def test_matches_by_date(self):
        """Действует между публикацией и исключением; другой ИНН — нет."""
        sup = {"inn": "1660331280", "kpp": "166001001"}
        self.assertEqual(len(cp.rnp_matches([self.entry], sup, date(2026, 6, 1))), 1)
        self.assertEqual(cp.rnp_matches([self.entry], sup, date(2026, 1, 1)), [])
        self.assertEqual(cp.rnp_matches([dict(self.entry, excluded_at="2026-05-01")], sup, date(2026, 6, 1)), [])
        self.assertEqual(cp.rnp_matches([self.entry], {"inn": "1"}, date(2026, 6, 1)), [])
        self.assertEqual(cp.rnp_matches([dict(self.entry, has_inclusion=0, status="excluded")], sup, date(2026, 6, 1)), [])

    def test_risk(self):
        """REG-002 с источником registry и ссылкой на запись."""
        r = cp.rnp_risks([self.entry])[0]
        self.assertEqual((r["rule_id"], r["source"]), ("REG-002", "registry"))
        self.assertEqual(r["evidence"][0]["src"], "rnp:7")

    def test_supplier_section(self):
        """Секция поставщика: действующая запись РНП → REG-002 и блок ``rnp``."""
        sec = org_profile.supplier_section([], [], "1660331280", settings, None, today=date(2026, 6, 1), rnp=[self.entry])
        self.assertEqual([x["rule_id"] for x in sec["risks"]], ["REG-002"])
        self.assertEqual(sec["risks"][0]["source"], "registry")
        self.assertEqual(sec["rnp"][0]["reg_number"], "РНП.123")


class DailyRunsPushTest(unittest.TestCase):
    """Сводка — в ``risk_monitoring_daily_runs``."""

    def test_url(self):
        """``/daily-runs`` рядом с ``/ai-analysis``."""
        try:
            from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        api = RiskMonitoringAPI.__new__(RiskMonitoringAPI)
        api.url = "https://stage.x/api/risk-monitoring/ai-analysis"
        self.assertEqual(api.daily_runs_url, "https://stage.x/api/risk-monitoring/daily-runs")

    def test_save_pushes(self):
        """Отметка «выполняется» — без сводки, готовая — со сводкой; сбой записи — сводка у нас с ошибкой."""
        from risk_monitoring import daily

        saved = []

        class Repo:
            async def save_rm_result(self, env, kind, ext_id, ai, pushed, err):
                saved.append((pushed, err))

        run = object.__new__(daily.DailyRun)
        run.http_manager, run.environment, run.env, run.repo, run.push = object(), "stage", "stage", Repo(), True
        api = mock.MagicMock()
        api.upsert_daily_run = mock.AsyncMock(return_value={"id": 1})
        run.risk_api = api
        asyncio.run(run.save("2026-10-19", {"pipeline": {"status": "running", "started_at": "t"}}))
        asyncio.run(run.save("2026-10-19", {"stages": {"xml": {}}, "pipeline": {"status": "completed"}}))
        self.assertIsNone(api.upsert_daily_run.call_args_list[0].args[5])
        self.assertEqual(api.upsert_daily_run.call_args_list[1].args[5]["stages"], {"xml": {}})
        api.upsert_daily_run.side_effect = RuntimeError("404")
        asyncio.run(run.save("2026-10-19", {"pipeline": {"status": "completed"}}))
        self.assertEqual(saved[:2], [(True, None), (True, None)])
        self.assertFalse(saved[2][0])
        self.assertIn("404", saved[2][1])


if __name__ == "__main__":
    unittest.main()

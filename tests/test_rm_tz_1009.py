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

    def test_primary_source_simple(self):
        """Две метки: «rule» — нашло правило (в т. ч. вместе с моделью), «ai» — только модель (27.10.2026)."""
        with mock.patch.object(settings, "RISK_SOURCE_LABELS", "simple"):
            self.assertEqual(passport.primary_source(["ai_file", "rule"]), "rule")
            self.assertEqual(passport.primary_source(["registry"]), "rule")
            self.assertEqual(passport.primary_source(["package", "ai_event"]), "rule")
            self.assertEqual(passport.primary_source(["ai_file", "version_diff", "ai_event"]), "ai")
            self.assertIsNone(passport.primary_source([]))

    @mock.patch.object(settings, "RISK_SOURCE_LABELS", "detailed")
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
        self.assertEqual(sec["risks"][0]["source"], "rule")
        self.assertEqual(sec["risks"][0]["sources"], ["registry"])
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
        """«Выполняется» — только POST состояния; готовая — POST (статус, этапы без ошибок, время с поясом),
        затем PATCH сводки по полученному id; сбой записи — сводка у нас с ошибкой."""
        from risk_monitoring import daily

        saved = []

        class Repo:
            async def save_rm_result(self, env, kind, ext_id, ai, pushed, err):
                saved.append((pushed, err))

        run = object.__new__(daily.DailyRun)
        run.http_manager, run.environment, run.env, run.repo, run.push = object(), "stage", "stage", Repo(), True
        api = mock.MagicMock()
        api.upsert_daily_run = mock.AsyncMock(return_value={"id": 12})
        api.patch_ai_analysis = mock.AsyncMock(return_value={})
        run.risk_api = api
        asyncio.run(run.save("2026-10-19", {"pipeline": {"status": "running", "started_at": "2026-10-19T06:00:00"}}))
        api.patch_ai_analysis.assert_not_called()
        first = api.upsert_daily_run.call_args_list[0]
        self.assertEqual(first.args[:2], ("2026-10-19", "running"))
        self.assertRegex(first.kwargs["started_at"], r"T06:00:00[+-]\d\d:\d\d$")
        self.assertIsNone(first.kwargs["finished_at"])
        ai = {"stages": {"xml": {"status": "failed", "error": "длинный текст"}},
              "pipeline": {"status": "completed", "started_at": "2026-10-19T06:00:00", "finished_at": "2026-10-19T07:00:00"}}
        asyncio.run(run.save("2026-10-19", ai))
        second = api.upsert_daily_run.call_args_list[1]
        self.assertEqual(second.kwargs["stages"], {"xml": {"status": "failed"}})
        self.assertTrue(second.kwargs["finished_at"])
        api.patch_ai_analysis.assert_awaited_once_with("risk_monitoring_daily_runs", 12, ai)
        api.upsert_daily_run.side_effect = RuntimeError("422")
        asyncio.run(run.save("2026-10-19", ai))
        self.assertEqual(saved[:2], [(True, None), (True, None)])
        self.assertFalse(saved[2][0])
        self.assertIn("422", saved[2][1])


class SuppliersTest(unittest.TestCase):
    """Поставщики закупок прогона → организации по API → профиль поставщика."""

    def test_supplier_rows(self):
        """Юрлица заказчиков на мониторинге без повторов; ИП (12 цифр) и без КПП — нет."""
        from risk_monitoring import daily

        cards = {1: {"monitoring": 1, "supplier_inn": "1660331280", "supplier_kpp": "166001001", "supplier_name": "СК"},
                 2: {"monitoring": 1, "supplier_inn": "1660331280", "supplier_kpp": "166001001", "supplier_name": "СК"},
                 3: {"monitoring": 1, "supplier_inn": "615492698364", "supplier_kpp": None, "supplier_name": "ИП"},
                 4: {"monitoring": 0, "supplier_inn": "7701234567", "supplier_kpp": "770101001", "supplier_name": "Х"}}
        self.assertEqual([r["inn"] for r in daily.supplier_rows(cards)], ["1660331280"])
        self.assertEqual(len(daily.supplier_rows(cards, monitored_only=False)), 2)

    def test_organisation_validation(self):
        """API организаций — только ИНН 10 и КПП 9 цифр."""
        try:
            from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        api = RiskMonitoringAPI.__new__(RiskMonitoringAPI)
        with self.assertRaises(ValueError):
            asyncio.run(api.upsert_organisation("615492698364", "", "ИП"))

    def test_summary_suppliers(self):
        """Поставщик с REG-002 — в списке поставщиков и во «внимании»."""
        from risk_monitoring import daily

        res = [{"organisation_id": 2082, "ai_analysis": {"organisation": {"name": "СК", "inn": "1660331280"},
                                                         "supplier": {"score": 70, "zone": "red",
                                                                      "risks": [{"rule_id": "REG-002"}]}}}]
        s = daily.build_summary(export_date="2026-10-19", environment="stage", cards={}, contracts={}, changed=[],
                                events={}, xml_cards={}, customers=[], scope={"xml": 1}, stages={}, suppliers=res)
        self.assertEqual(s["suppliers"][0]["risks"], ["REG-002"])
        self.assertEqual(s["attention"][0]["kind"], "supplier_registry")

if __name__ == "__main__":
    unittest.main()

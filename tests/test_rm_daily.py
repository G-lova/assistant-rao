"""Ежедневный прогон риск-мониторинга и сводка (``risk_monitoring.daily``)."""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest import mock

from risk_monitoring import daily, settings


def _contract(score, zone, prev=None, delta=None, registry=False):
    """``ai_analysis`` закупки для сводки."""
    return {"passport": {"purchase_number": "0319", "subject": "Поставка", "customer": {"name": "ЗГУ"}},
            "profile": {"score": score, "zone": zone, "previous_score": prev, "delta": delta,
                        "top_factors": [{"rule_id": "DOC-002", "title": "Товарный знак"}]},
            "registry": {"matches": [{"black_list_id": 1}] if registry else []}}


class DailySummaryTest(unittest.TestCase):
    """Сборка сводки без модели."""

    def setUp(self):
        """Две закупки заказчика на мониторинге и одна — вне мониторинга."""
        self.cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"},
                      2: {"id": 2, "number": "N2", "organisation_id": 10, "monitoring": "1", "customer_name": "ЗГУ"},
                      3: {"id": 3, "number": "N3", "organisation_id": 11, "monitoring": 0, "customer_name": "Другой"}}
        self.contracts = {1: _contract(75, "red", prev=50, delta=25), 2: _contract(30, "green", registry=True),
                          3: _contract(90, "red", prev=10, delta=80)}
        self.events = {101: {"event": {"code": "CON-003", "title": "Расторжение контракта", "importance": "high"},
                             "risks": [{"rule_id": "CTR-020"}], "resume": "Расторгнут"},
                       102: {"event": {"code": "PUR-001", "title": "Размещено извещение", "importance": "low"}, "risks": []},
                       103: {"event": {"code": "CON-003", "title": "Расторжение контракта", "importance": "high"}, "risks": []}}
        self.xml_cards = {101: 1, 102: 2, 103: 3}

    def build(self, **kw):
        """Сводка по данным теста."""
        args = dict(export_date="2026-10-20", environment="stage", cards=self.cards, contracts=self.contracts,
                    changed=[1, 3], events=self.events, xml_cards=self.xml_cards, customers=[],
                    scope={"xml": 3, "files": 2, "contracts": 3}, stages={"files": {"status": "completed"}})
        args.update(kw)
        return daily.build_summary(**args)

    def test_monitored_only(self):
        """Закупка заказчика вне мониторинга в сводку не попадает."""
        s = self.build()
        self.assertEqual(s["scope"]["contracts_monitored"], 2)
        self.assertEqual(s["contracts"]["by_zone"], {"green": 1, "yellow": 0, "red": 1})
        self.assertEqual(s["events"]["total"], 2)
        self.assertNotIn(3, [a["card_id"] for a in s["attention"]])

    def test_attention(self):
        """Реестр 19.28, переход в красную зону, рост балла, важное событие."""
        kinds = [(a["kind"], a["card_id"]) for a in self.build()["attention"]]
        self.assertEqual(kinds, [("registry_1928", 2), ("became_red", 1), ("score_jump", 1), ("event", 1)])

    def test_unchanged_delta_hidden(self):
        """У закупки, не пересчитанной в этом прогоне, прошлое изменение балла не показывается."""
        s = self.build(changed=[])
        self.assertEqual(s["contracts"]["score_jump"], [])
        self.assertEqual(s["contracts"]["became_red"], [])

    def test_facts(self):
        """Факты: охват, зоны, внимание, сбой этапа; без внимания — явная фраза."""
        s = self.build(stages={"xml": {"status": "failed"}})
        facts = " ".join(daily.summary_facts(s))
        self.assertIn("красная 1, жёлтая 0, зелёная 1", facts)
        self.assertIn("перешла в красную зону: балл 75 (был 50)", facts)
        self.assertIn("Не завершились этапы: xml", facts)
        quiet = self.build(contracts={2: _contract(30, "green")}, events={})
        self.assertIn("резкого роста балла", " ".join(daily.summary_facts(quiet)))

    def test_empty_day(self):
        """День без документов заказчиков на мониторинге."""
        s = self.build(cards={}, contracts={}, events={}, xml_cards={})
        self.assertIn("новых документов нет", daily.default_resume(s))


class DailyStateTest(unittest.TestCase):
    """Ключ сводки и защита от повторного запуска."""

    def test_run_key(self):
        """Дата → ГГГГММДД; неверная дата — ошибка."""
        self.assertEqual(daily.run_key("2026-10-20"), 20261020)
        with self.assertRaises(ValueError):
            daily.run_key("20.10.2026")

    def test_running(self):
        """Свежая отметка «выполняется» блокирует второй запуск, старая — нет; готовая сводка — completed."""
        now = datetime(2026, 10, 20, 7, 0)
        fresh = {"ai_analysis": {"pipeline": {"status": "running", "started_at": (now - timedelta(hours=1)).isoformat()}}}
        stale = {"ai_analysis": {"pipeline": {"status": "running", "started_at": (now - timedelta(hours=5)).isoformat()}}}
        self.assertTrue(daily.running_fresh(fresh, now))
        self.assertFalse(daily.running_fresh(stale, now))
        self.assertFalse(daily.running_fresh(None, now))
        self.assertTrue(daily.completed({"ai_analysis": {"pipeline": {"status": "completed"}}}))

    def test_monitored_values(self):
        """``monitoring`` из MySQL: 1, 1.0, "1", True — на мониторинге; 0, None — нет."""
        for v in (1, 1.0, "1", True, "true"):
            self.assertTrue(daily.monitored({"monitoring": v}), v)
        for v in (0, None, "0", ""):
            self.assertFalse(daily.monitored({"monitoring": v}), v)


class DailyRunTest(unittest.TestCase):
    """Прогон целиком на подменённых анализаторах: этапы по порядку, сбой этапа не останавливает прогон."""

    def test_run(self):
        """Файлы → события → закупки (без анализа документов) → заказчики → сводка; сбой заказчиков — failed."""
        saved = []

        class Repo:
            """Хранилище результатов в памяти."""

            async def get_rm_results(self, env, kind, ids):
                """Сохранённые закупки."""
                if kind == "contract":
                    return {1: {"ai_analysis": _contract(80, "red", prev=60, delta=20), "status": "completed"}}
                return {}

            async def save_rm_result(self, env, kind, ext_id, ai, pushed, err):
                """Запоминает сводку."""
                saved.append((kind, ext_id, ai["pipeline"]["status"]))

        run = object.__new__(daily.DailyRun)
        run.http_manager, run.environment, run.env, run.repo = None, "stage", "stage", Repo()
        run.push, run.use_llm, run.stages_on, run.stages, run.llm_stats = False, False, daily.STAGES, {}, {}

        async def query(name, head=(), **lists):
            """Ответы внешней БД."""
            if name == "daily_xml.sql":
                return [{"id": 101, "card_id": 1}]
            if name == "daily_cards.sql":
                return [{"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}]
            return []

        run._query = query
        calls = []
        xa = mock.MagicMock()
        xa.pending_files = mock.AsyncMock(return_value=[5])
        xa.analyze_ids = mock.AsyncMock(return_value=[{"xml_id": 101, "status": "done", "ai_analysis": {
            "event": {"code": "CON-003", "title": "Расторжение", "importance": "high"}, "risks": []}}])
        fa = mock.MagicMock()
        fa.analyze_ids = mock.AsyncMock(return_value=[{"file_id": 5, "status": "done", "ai_analysis": {"risks": []}}])
        ca = mock.MagicMock()
        ca.analyze_ids = mock.AsyncMock(return_value=[{"card_id": 1, "status": "done"}])

        def contract_cls(*a, **kw):
            """ContractAnalyzer: документы повторно не анализируются."""
            calls.append(kw.get("analyze_documents"))
            return ca

        oa = mock.MagicMock()
        oa.analyze_ids = mock.AsyncMock(side_effect=RuntimeError("нет БД"))
        with mock.patch("risk_monitoring.xml_event_analyzer.XmlEventAnalyzer", return_value=xa), \
                mock.patch("risk_monitoring.ai_file_analyzer.AIFileAnalyzer", return_value=fa), \
                mock.patch("risk_monitoring.contract_analyzer.ContractAnalyzer", side_effect=contract_cls), \
                mock.patch("risk_monitoring.org_analyzer.OrgAnalyzer", return_value=oa):
            s = asyncio.run(run.run("2026-10-20"))
        self.assertEqual(calls, [False])
        self.assertEqual(xa.file_results, {5: {"risks": []}})
        self.assertEqual(s["stages"]["customers"]["status"], "failed")
        self.assertEqual(s["stages"]["contracts"]["status"], "completed")
        self.assertEqual([a["kind"] for a in s["attention"]], ["became_red", "score_jump", "event"])
        self.assertEqual(saved, [("daily", 20261020, "running"), ("daily", 20261020, "completed")])
        self.assertTrue(s["resume"])


if __name__ == "__main__":
    unittest.main()

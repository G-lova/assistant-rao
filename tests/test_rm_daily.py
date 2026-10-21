"""Ежедневный прогон риск-мониторинга и сводка (``risk_monitoring.daily``)."""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest import mock

from configs import settings
from risk_monitoring import daily


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
        """Отметка «выполняется» со свежим пульсом блокирует второй запуск, без пульса 10+ минут (контейнер
        перезапущен) — нет; готовая сводка — completed."""
        now = datetime(2026, 10, 20, 7, 0)
        fresh = {"ai_analysis": {"pipeline": {"status": "running", "started_at": (now - timedelta(hours=3)).isoformat(),
                                              "heartbeat_at": (now - timedelta(minutes=2)).isoformat()}}}
        stale = {"ai_analysis": {"pipeline": {"status": "running", "started_at": (now - timedelta(hours=1)).isoformat(),
                                              "heartbeat_at": (now - timedelta(minutes=30)).isoformat()}}}
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
        run.http_manager, run.environment, run.env, run.repo, run.storage_path = None, "stage", "stage", Repo(), "s"
        run.push, run.use_llm, run.stages_on, run.stages, run.llm_stats = False, False, daily.STAGES, {}, {}

        async def query(name, head=(), **lists):
            """Ответы внешней БД."""
            queried.append(name)
            if name == "daily_xml.sql":
                return [{"id": 101, "card_id": 1, "risk_monitoring_contract_id": 1, "monitoring": 1}]
            if name == "daily_files.sql":
                return [{"id": 5, "xml_source_id": 101, "card_id": 1}]
            if name == "daily_cards.sql":
                return [{"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}]
            return []

        queried = []
        run._query = query

        class Arch:
            """Архивы без скачивания."""
            closed = False

            def close(self):
                """Отметка удаления архивов."""
                Arch.closed = True

        run._make_archives = Arch
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
        self.assertEqual(xa.file_results, {5: {"doc_type": {"detected_type": None}, "resume": None,
                                               "total_doc_risk": None}})
        self.assertEqual(queried, ["daily_xml.sql", "xml_previous.sql", "daily_files.sql", "daily_cards.sql"])
        self.assertIn(5, run.cache.file_rows)
        self.assertIn(1, run.cache.covered_cards)
        self.assertEqual(s["stages"]["customers"]["status"], "failed")
        self.assertEqual(s["stages"]["contracts"]["status"], "completed")
        self.assertEqual([a["kind"] for a in s["attention"]], ["became_red", "score_jump", "event"])
        self.assertEqual(saved, [("daily", 20261020, "running"), ("daily", 20261020, "completed")])
        self.assertTrue(run._finished)
        self.assertTrue(s["resume"])
        self.assertTrue(Arch.closed)
        self.assertIn("external_queries", s["pipeline"])

    def test_no_exports_fact(self):
        """За дату выгрузок нет — сводка называет ближайшие выгрузки с XML."""
        s = daily.build_summary(export_date="2026-05-08", environment="stage", cards={}, contracts={}, changed=[],
                                events={}, xml_cards={}, customers=[], stages={},
                                scope={"xml": 0, "files": 0, "contracts": 0,
                                       "nearest_exports": [{"date": "2026-05-12", "archives": 2, "xml": 40}]})
        facts = " ".join(daily.summary_facts(s))
        self.assertIn("день выгрузки, а не публикации", facts)
        self.assertIn("2026-05-12 (XML 40)", facts)


class ScopeTest(unittest.TestCase):
    """Охват анализа за дату."""

    def test_choose_scope(self):
        """Обычная дата — все XML; массовая загрузка — только заказчики на мониторинге; настройка — всегда они."""
        with mock.patch.object(settings, "DAILY_SCOPE", "all"), mock.patch.object(settings, "DAILY_ALL_MAX_XML", 5000):
            self.assertEqual(daily.choose_scope(3773), "all")
            self.assertEqual(daily.choose_scope(21907), "monitored_auto")
        with mock.patch.object(settings, "DAILY_SCOPE", "all"), mock.patch.object(settings, "DAILY_ALL_MAX_XML", None):
            self.assertEqual(daily.choose_scope(21907), "all")
        with mock.patch.object(settings, "DAILY_SCOPE", "monitored"):
            self.assertEqual(daily.choose_scope(10), "monitored")


class RunCacheTest(unittest.TestCase):
    """Кэш прогона: запрашиваются только новые id; засеянные запросом по дате — не запрашиваются."""

    def test_keyed_and_seed(self):
        """Второй запрос тех же id — из кэша; засеянные id — без запроса; пачки по CHUNK."""
        from risk_monitoring import run_cache
        from risk_monitoring.run_cache import RunCache

        calls = []

        async def fetch(name, head=(), **lists):
            calls.append(list(lists["ids"]))
            return [{"id": i} for i in lists["ids"]]

        c = RunCache()
        c.seed("xml_meta.sql", [{"id": 1}], "id", [1, 2])
        got = asyncio.run(c.one_each(fetch, "xml_meta.sql", [1, 2, 3], "id"))
        self.assertEqual(calls, [[3]])
        self.assertEqual(sorted(got), [1, 3])
        asyncio.run(c.one_each(fetch, "xml_meta.sql", [3, 1], "id"))
        self.assertEqual(len(calls), 1)
        with mock.patch.object(run_cache, "CHUNK", 2):
            asyncio.run(c.keyed(fetch, "other.sql", [10, 11, 12], "id"))
        self.assertEqual(calls[1:], [[10, 11], [12]])


class XmlBatchTest(unittest.TestCase):
    """Пакетные запросы анализатора событий: один запрос на шаг для всех XML, повтор — из кэша."""

    def test_prefetch_batches(self):
        """Три XML без закупки: файлы — один запрос, привязка — по запросу на шаг; второй раз — без запросов."""
        try:
            from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        from risk_monitoring.run_cache import RunCache

        xa = XmlEventAnalyzer.__new__(XmlEventAnalyzer)
        xa.storage_path, xa.force, xa.internal, xa.results_repo = "s", False, {}, None
        body = (b'<contract><commonInfo><number>N</number></commonInfo>'
                b'<foundation><purchaseNumber>{n}</purchaseNumber></foundation></contract>')
        bodies = {f"e{i}": body.replace(b"{n}", f"037310000{i}".encode()) for i in range(3)}

        class Arch:
            async def read(self, url, entry):
                return bodies[entry]

            def close(self):
                pass

        xa.cache = RunCache(Arch())
        xa.archives = xa.cache.archives
        calls = []

        async def query(name, head=(), **lists):
            """Внешняя БД: запоминает запросы."""
            calls.append(name)
            if name == "link_by_number.sql":
                return [{"id": 70, "number": "0373100001", "risk_monitoring_organisation_id": 5}]
            return []

        xa._query = query
        rows = [{"id": 10 + i, "archive_url": "a", "entry_name": f"e{i}", "risk_monitoring_contract_id": None,
                 "purchase_number": f"037310000{i}"} for i in range(3)]
        asyncio.run(xa.prefetch(rows, {}))
        self.assertEqual(calls.count("xml_files.sql"), 1)
        self.assertEqual(calls.count("link_by_number.sql"), 1)
        self.assertLessEqual(calls.count("link_by_contract_number.sql"), 1)
        self.assertEqual(xa.cache.links[11]["risk_monitoring_contract_id"], 70)
        self.assertEqual(xa.cache.links[10]["method"], "none")
        before = len(calls)
        link = asyncio.run(xa._link_orphan({}, rows[1]))
        asyncio.run(xa._files(11, None, []))
        self.assertEqual(link["risk_monitoring_contract_id"], 70)
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()


class HeartbeatTest(unittest.TestCase):
    """Пульс отметки «выполняется»: обновляется, пока прогон идёт, и не перезаписывает итоговую сводку."""

    def test_heartbeat(self):
        """Пульс пишет heartbeat_at; после итоговой сводки — больше не пишет."""
        saved = []

        class Repo:
            async def save_rm_result(self, env, kind, ext_id, ai, pushed, err):
                saved.append(dict(ai["pipeline"]))

        run = object.__new__(daily.DailyRun)
        run.repo, run.env, run.push, run.http_manager = Repo(), "stage", False, None
        marker = {"pipeline": {"status": "running", "started_at": "2026-10-21T18:38:49", "heartbeat_at": "x"}}

        async def go():
            with mock.patch.object(daily, "HEARTBEAT_SECONDS", 0.01):
                beat = asyncio.ensure_future(run._heartbeat("2026-07-03", marker))
                await asyncio.sleep(0.05)
                await run.save("2026-07-03", {"pipeline": {"status": "completed"}})
                n = len(saved)
                await asyncio.sleep(0.05)
                beat.cancel()
                return n

        n = asyncio.run(go())
        self.assertGreaterEqual(n, 2)
        self.assertEqual(saved[n - 1]["status"], "completed")
        self.assertEqual(len(saved), n)
        self.assertNotEqual(saved[0]["heartbeat_at"], "x")

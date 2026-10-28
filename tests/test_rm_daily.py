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
            "registry": [{"black_list_id": 1}] if registry else []}


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

    def build_monitored(self, **kw):
        """Сводка в прежнем режиме: только заказчики с monitoring = 1."""
        cfg = mock.Mock(wraps=settings)
        for k in dir(settings):
            if k.isupper():
                setattr(cfg, k, getattr(settings, k))
        cfg.DAILY_MONITORED_ONLY = True
        return self.build(cfg=cfg, **kw)

    def test_all_system_purchases(self):
        """Все закупки системы в сводке; заказчики с задачами мониторинга посчитаны и отмечены отдельно."""
        s = self.build()
        self.assertEqual(s["scope"]["contracts_in_scope"], 3)
        self.assertEqual(s["scope"]["contracts_monitored"], 2)
        self.assertEqual(s["scope"]["customers_monitored"], 1)
        self.assertEqual(s["contracts"]["by_zone"], {"green": 1, "yellow": 0, "red": 2})
        self.assertEqual(s["events"]["total"], 3)
        self.assertIn(3, [a["card_id"] for a in s["attention"]])
        self.assertFalse(next(a for a in s["attention"] if a["card_id"] == 3)["monitoring"])

    def test_monitored_only(self):
        """С DAILY_MONITORED_ONLY закупка заказчика без задач мониторинга в сводку не попадает."""
        s = self.build_monitored()
        self.assertEqual(s["scope"]["contracts_monitored"], 2)
        self.assertEqual(s["contracts"]["by_zone"], {"green": 1, "yellow": 0, "red": 1})
        self.assertEqual(s["events"]["total"], 2)
        self.assertNotIn(3, [a["card_id"] for a in s["attention"]])

    def test_attention(self):
        """Реестр 19.28, переход в красную зону, рост балла, важное событие."""
        kinds = [(a["kind"], a["card_id"]) for a in self.build_monitored()["attention"]]
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
        self.assertIn("красная 2, жёлтая 0, зелёная 1", facts)
        self.assertIn("(заказчик с текущими задачами мониторинга)", facts)
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
        self.assertTrue(daily.completed({"ai_analysis": {"pipeline": {"status": "completed",
                                                                      "schema_version": daily.SCHEMA_VERSION}}}))
        # сводка прежних правил охвата устарела — прогон за дату выполняется снова без force
        self.assertFalse(daily.completed({"ai_analysis": {"pipeline": {"status": "completed",
                                                                       "schema_version": "rm-daily-1"}}}))

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
        self.assertEqual(queried, ["daily_xml.sql", "xml_previous.sql", "daily_files.sql", "own_scope.sql",
                                   "daily_cards.sql"])
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
        """Обычная дата — все XML; массовая загрузка — только XML закупок системы; настройка — всегда они."""
        with mock.patch.object(settings, "DAILY_SCOPE", "all"), mock.patch.object(settings, "DAILY_ALL_MAX_XML", 5000):
            self.assertEqual(daily.choose_scope(3773), "all")
            self.assertEqual(daily.choose_scope(21907), "own_auto")
        with mock.patch.object(settings, "DAILY_SCOPE", "all"), mock.patch.object(settings, "DAILY_ALL_MAX_XML", None):
            self.assertEqual(daily.choose_scope(21907), "all")
        with mock.patch.object(settings, "DAILY_SCOPE", "own"):
            self.assertEqual(daily.choose_scope(10), "own")


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


class FirstEvaluationTest(unittest.TestCase):
    """Первая оценка закупки в красной зоне — не «перешла в красную зону»; число пунктов — отдельным фактом."""

    def test_red_first(self):
        """Без прошлого балла — red_first; в фактах — сводная строка с числами."""
        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}}
        s = daily.build_summary(export_date="2026-07-03", environment="stage", cards=cards,
                                contracts={1: _contract(72, "red")}, changed=[1], events={}, xml_cards={},
                                customers=[], scope={"xml": 1}, stages={})
        self.assertEqual([a["kind"] for a in s["attention"]], ["red_first"])
        facts = " ".join(daily.summary_facts(s))
        self.assertIn("в красной зоне при первой оценке — 1", facts)
        self.assertNotIn("перешла в красную", facts)


class PlainNamesTest(unittest.TestCase):
    """В сводке — названия рисков, а не коды."""

    def test_no_codes_in_facts(self):
        """Заказчики с рисками и события с рисками — без кодов вида BEH-005, CTR-020."""
        import re

        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}}
        events = {5: {"event": {"code": "CON-007", "title": "Расторжение контракта", "importance": "high"},
                      "risks": [{"rule_id": "CTR-020"}]}}
        cust = [{"organisation_id": 10, "ai_analysis": {"organisation": {"name": "ЗГУ"},
                                                        "customer": {"score": 50, "zone": "yellow",
                                                                     "risks": [{"rule_id": "BEH-005"}, {"rule_id": "BEH-001"}]}}}]
        s = daily.build_summary(export_date="2026-07-03", environment="stage", cards=cards, contracts={},
                                changed=[], events=events, xml_cards={5: 1}, customers=cust, scope={"xml": 1}, stages={})
        facts = " ".join(daily.summary_facts(s))
        self.assertIsNone(re.search(r"\b[A-Z]{2,5}-\d{3}\b", facts), facts)
        self.assertIn("контракты сосредоточены у немногих поставщиков", facts)


class TestEventType(unittest.TestCase):
    """``ai_analysis.event.event_type`` обязателен для API (422 от 23.10.2026)."""

    def test_fills_from_code(self):
        """Код события становится ``event_type``; уже заданный не перезаписывается."""
        from risk_monitoring.xml_events import with_event_type
        self.assertEqual(with_event_type({"code": "PUR-001"})["event_type"], "PUR-001")
        self.assertEqual(with_event_type({"xml_tag": "epNotificationEZK2020"})["event_type"], "epNotificationEZK2020")
        self.assertEqual(with_event_type({"code": "A", "event_type": "B"})["event_type"], "B")

    def test_patch_adds_event_type_to_stored(self):
        """Сохранённый до исправления результат дозаписывается с ``event_type``."""
        from tests.stubs import stubbed_import
        sent = {}

        async def fake(method, json=None, **kw):
            """Запоминает тело запроса."""
            sent.update(json)
            return {"data": {}}

        with stubbed_import("risk_monitoring.risk_monitoring_api") as m:
            api = m.RiskMonitoringAPI.__new__(m.RiskMonitoringAPI)
            with mock.patch.object(m.RiskMonitoringAPI, "_check_table", lambda self, t: None), \
                    mock.patch.object(api, "_request", fake, create=True):
                asyncio.run(api.patch_ai_analysis("risk_monitoring_eis_xml_sources", 1, {"event": {"code": "PUR-001"}}))
        self.assertEqual(sent["ai_analysis"]["event"]["event_type"], "PUR-001")


class RegistryFormatTest(unittest.TestCase):
    """``ai_analysis.registry`` закупки — список совпадений (422 API от 23.10.2026)."""

    def test_normalize_old(self):
        """Прежний объект превращается в список; признак проверки и РНП — отдельными полями."""
        from risk_monitoring.contract_package import normalize_registry, stored_registry
        ai = {"registry": {"checked": True, "matches": [{"black_list_id": 5}], "rnp": [{"rnp_entry_id": 1}]}}
        self.assertEqual(stored_registry(ai), [{"black_list_id": 5}])
        normalize_registry(ai)
        self.assertEqual(ai["registry"], [{"black_list_id": 5}])
        self.assertTrue(ai["registry_checked"])
        self.assertEqual(ai["rnp"], [{"rnp_entry_id": 1}])
        self.assertEqual(stored_registry(ai), [{"black_list_id": 5}])

    def test_running_clears_finished(self):
        """Отметка «выполняется» сбрасывает окончание прежнего запуска."""
        from tests.stubs import stubbed_import
        sent = {}

        async def fake(method, url=None, json=None, **kw):
            """Запоминает тело."""
            sent.update(json)
            return {"data": {"id": 1}}

        with stubbed_import("risk_monitoring.risk_monitoring_api") as m:
            api = m.RiskMonitoringAPI.__new__(m.RiskMonitoringAPI)
            api.url = "https://x/api/risk-monitoring/ai-analysis"
            with mock.patch.object(api, "_request", fake, create=True):
                asyncio.run(api.upsert_daily_run("2026-06-25", "running", started_at="2026-10-23T13:20:00+03:00"))
        self.assertIn("finished_at", sent)
        self.assertIsNone(sent["finished_at"])


class QuietDayTest(unittest.TestCase):
    """Пустой по мониторингу день — сводка шаблоном, без модели."""

    def test_quiet(self):
        """Нет закупок, событий и заказчиков на мониторинге — тихий день; есть пункт внимания — нет."""
        s = {"scope": {"xml": 90, "contracts_monitored": 0, "events_monitored": 0, "customers_monitored": 0},
             "attention": [], "suppliers": []}
        self.assertTrue(daily.quiet_day(s))
        s["attention"] = [{"kind": "supplier_registry"}]
        self.assertFalse(daily.quiet_day(s))
        self.assertFalse(daily.quiet_day({"scope": {"contracts_monitored": 2}}))


class MergeStringFieldsTest(unittest.TestCase):
    """Объединение блоков извлечения не падает, если модель отдала строку вместо объекта."""

    def test_as_obj(self):
        """Строка — в объект под ключом; объект — как есть; пусто — пустой объект."""
        from tests.stubs import stubbed_import
        with stubbed_import("evaluate_documents.type_data_extractor") as m:
            self.assertEqual(m._as_obj("Контракт", "value"), {"value": "Контракт"})
            self.assertEqual(m._as_obj({"value": 1}, "value"), {"value": 1})
            self.assertEqual(m._as_obj(None, "value"), {})
            self.assertEqual(m._as_obj("", "value"), {})


class OwnLLMTest(unittest.TestCase):
    """Модель разбирает события закупок системы (все подведомственные организации), а не только monitoring = 1."""

    def test_filter(self):
        """Привязанный XML, «сирота», найденная к закупке или к организации системы, — да; чужой заказчик — нет."""
        ok = daily.make_own_filter({7})
        self.assertTrue(ok({"row": {"risk_monitoring_contract_id": 11, "monitoring": 0}}))
        self.assertTrue(ok({"row": {}, "link": {"risk_monitoring_contract_id": 10}}))
        self.assertTrue(ok({"row": {}, "link": {"organisation_id": 7}}))
        self.assertFalse(ok({"row": {"risk_monitoring_contract_id": None}, "link": {"organisation_id": 8}}))

    def test_defer(self):
        """XML чужого заказчика — модель откладывается с причиной; появилась закупка — разбирается повторно."""
        from tests.stubs import stubbed_import
        with stubbed_import("risk_monitoring.xml_event_analyzer") as m:
            xa = m.XmlEventAnalyzer.__new__(m.XmlEventAnalyzer)
            xa.llm_filter = daily.make_own_filter(set())
            ctxs = [{"needs_llm": True, "llm_reason": "importance", "row": {}, "link": None},
                    {"needs_llm": True, "llm_reason": "rule_risks", "row": {"risk_monitoring_contract_id": 5},
                     "link": None}]
            xa._defer_llm(ctxs)
            self.assertFalse(ctxs[0]["needs_llm"])
            self.assertEqual((ctxs[0]["llm_reason"], ctxs[0]["llm_deferred"]), ("not_own", "importance"))
            self.assertTrue(ctxs[1]["needs_llm"])
            self.assertTrue(xa._deferred_now_needed({"rm_llm_deferred": "importance", "risk_monitoring_contract_id": 5}))
            self.assertFalse(xa._deferred_now_needed({"rm_llm_deferred": "importance"}))
            self.assertFalse(xa._deferred_now_needed({"rm_llm_deferred": None, "risk_monitoring_contract_id": 5}))


class ArchiveOcrShrinkTest(unittest.TestCase):
    """OCR страниц из архивов: переполнение контекста — уменьшение изображения и повтор, а не «модель недоступна»."""

    def test_shrink_and_retry(self):
        """Первый ответ — 400 «exceeds context», второй — текст; изображение уменьшено."""
        import os
        import tempfile
        from tests.stubs import stubbed_import
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("нет PIL")
        with tempfile.TemporaryDirectory() as tmp, stubbed_import("evaluate_documents.ocr") as m:
            path = os.path.join(tmp, "page_1.jpg")
            Image.new("RGB", (3000, 3000), "white").save(path, "JPEG")
            p = m.OCRProcessor.__new__(m.OCRProcessor)
            p.ocr_prompt, p.OCR_SCHEMA, p.model = "", {}, "x"

            async def acquire():
                """Без ограничения скорости."""

            p.rate_limiter = mock.Mock(acquire=acquire)
            calls = []

            async def once(image_path, name, image_name):
                """Первый вызов — переполнение, второй — ответ."""
                calls.append(Image.open(image_path).size)
                return ("err", True) if len(calls) == 1 else ('{"raw_text": "ok"}', False)

            p._ocr_once = once
            out = asyncio.run(m.OCRProcessor.ocr_image_with_qwen_vl.__wrapped__(p, path, "ПСД.rar"))
            self.assertEqual(out, '{"raw_text": "ok"}')
            self.assertLessEqual(calls[0][0] * calls[0][1], m.OCR_MAX_PIXELS)
            self.assertLess(calls[1][0], calls[0][0])


class SummaryScopeFixTest(unittest.TestCase):
    """Сводка: события только закупок системы, закупки в красной зоне — в фактах; delta заказчика при смене модели."""

    def test_orphan_events_excluded(self):
        """Событие XML без закупки не входит в сводку и во «внимание»."""
        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 0, "customer_name": "ЗГУ"}}
        s = daily.build_summary(export_date="2026-07-02", environment="stage", cards=cards,
                                contracts={1: _contract(80, "red")}, changed=[], xml_cards={1: 1, 2: None},
                                events={1: {"event": {"title": "Доп. соглашение", "importance": "high"}, "risks": []},
                                        2: {"event": {"title": "Заключён контракт", "importance": "high"}, "risks": []}},
                                customers=[], scope={"xml": 2}, stages={})
        self.assertEqual(s["events"]["total"], 1)
        self.assertEqual([a["xml_id"] for a in s["attention"]], [1])
        self.assertIn("В красной зоне (всего 1): закупка 0319", " ".join(daily.summary_facts(s)))

    def test_previous_customer_score(self):
        """Балл прежней модели не даёт delta; той же — даёт."""
        from risk_monitoring import org_profile
        with mock.patch.object(settings, "CUSTOMER_SCORE_MODEL", "quality"):
            self.assertIsNone(org_profile.previous_customer_score({"customer": {"score": 32}}))
            same = {"customer": {"score": 12, "score_model": {"version": org_profile.CUSTOMER_SCORE_VERSION}}}
            self.assertEqual(org_profile.previous_customer_score(same), 12)


class Fixes1026Test(unittest.TestCase):
    """Прогоны 26.10.2026: event_at протоколов, даты типовых положений, PDF Word по текстовому слою."""

    def test_event_at_fallback(self):
        """Нет даты в документе — дата выгрузки; сохранённый результат дополняется при записи."""
        from risk_monitoring import xml_events
        ai = {"event": {"code": "PUR-010"}, "pipeline": {"export_date": "2026-07-06"}}
        xml_events.with_required_event(ai)
        self.assertEqual(ai["event"]["event_at"], "2026-07-06T00:00:00+03:00")
        self.assertEqual(ai["event"]["event_at_source"], "export_date")
        self.assertEqual(ai["event"]["event_type"], "PUR-010")
        keep = {"event": {"code": "X", "event_at": "2026-07-01T10:00:00"}, "pipeline": {"export_date": "2026-07-06"}}
        self.assertEqual(xml_events.with_required_event(keep)["event"]["event_at"], "2026-07-01T10:00:00")

    def test_pg_dates(self):
        """Строки дат превращаются в date / datetime для asyncpg."""
        import datetime as dt
        from tests.stubs import stubbed_import
        with stubbed_import("risk_monitoring.typal_clauses") as m:
            self.assertEqual(m.pg_date("2026-10-19"), dt.date(2026, 10, 19))
            self.assertIsNone(m.pg_date("не дата"))
            self.assertEqual(m.pg_timestamp("2026-10-19T10:00:00Z").tzinfo, dt.timezone.utc)
            self.assertEqual(m.pg_timestamp("2026-10-19").date(), dt.date(2026, 10, 19))

    def test_rm_reader_uses_text_layer(self):
        """Чтение PDF (и Word через конвертацию) — по слою; общий OCR всех страниц только при сбое."""
        from tests.stubs import stubbed_import
        with stubbed_import("risk_monitoring.rm_file_reader") as m:
            r = m.RMFileReader.__new__(m.RMFileReader)
            r.ocr_processor = None

            async def layer(path, name, proc):
                """Текст слоя."""
                return {"text": "Страница 1:\nтекст"}

            with mock.patch.object(m.pdf_text, "read_pdf", layer):
                self.assertEqual(asyncio.run(r.read_pdf_file("x.pdf", "Карта.docx")), "Страница 1:\nтекст")


class BatchHardStopTest(unittest.TestCase):
    """Пачка, не уложившаяся в бюджет и запас, прерывается — прогон доходит до закупок и сводки."""

    def test_cut(self):
        """Файлы пачки дольше предела → partial, interrupted; события этой пачки не начинаются."""
        import sys
        import types
        from risk_monitoring.run_cache import RunCache

        class SlowFiles:
            """Анализ файлов, который не успевает."""

            def __init__(self, *a, **kw):
                """Без зависимостей."""

            async def analyze_ids(self, ids):
                """Дольше предела."""
                await asyncio.sleep(5)
                return []

        xa = mock.Mock()

        async def pending(part):
            """Файлы пачки."""
            return [1, 2]

        xa.pending_files = pending
        run = daily.DailyRun.__new__(daily.DailyRun)
        run.stages_on, run.stages, run.push, run.http_manager, run.environment, run.repo = (
            daily.STAGES, {}, False, None, "stage", None)
        fake = types.ModuleType("risk_monitoring.ai_file_analyzer")
        fake.AIFileAnalyzer = SlowFiles
        with mock.patch.dict(sys.modules, {"risk_monitoring.ai_file_analyzer": fake}), \
                mock.patch.object(settings, "DAILY_DOCUMENTS_BUDGET_SEC", 0.05), \
                mock.patch.object(settings, "DAILY_BATCH_OVERRUN_SEC", 0.2):
            asyncio.run(run._documents([1, 2, 3], xa, RunCache(), {"files": 0}, {}, {}))
        self.assertEqual(run.stages["files"]["status"], "partial")
        self.assertTrue(run.stages["files"]["interrupted"])
        self.assertEqual(run.stages["xml"]["status"], "partial")


class MergeUtilsTest(unittest.TestCase):
    """Поля ответа модели приводятся к ожидаемым типам при объединении блоков (data_extractor, 26.10.2026)."""

    def test_as_evidence_and_text(self):
        """Строка и список вместо объекта evidence; список вместо строки summary."""
        from evaluate_documents.merge_utils import as_evidence, as_obj, as_text
        self.assertEqual(as_evidence("п. 3.1"), {"fragment": "п. 3.1"})
        self.assertEqual(as_evidence([{"fragment": "a"}]), {"fragment": "a"})
        self.assertEqual(as_evidence(["a", "b"]), {"fragment": "a; b"})
        self.assertIsNone(as_evidence(None))
        self.assertEqual(as_obj("Контракт", "value"), {"value": "Контракт"})
        self.assertEqual(as_text(["a", "b"]), "a b")


class ContinuationTest(unittest.TestCase):
    """partial и failed доделываются в тот же день: продолжение после запуска и страховка по расписанию."""

    def _cfg(self, **kw):
        """Настройки продолжения."""
        import types
        base = dict(DAILY_CONTINUE_ENABLED=True, DAILY_CONTINUE_DELAY_SEC=60, DAILY_RETRY_FAILED_DELAY_SEC=900,
                    DAILY_CONTINUE_MAX_ATTEMPTS=3)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_delay(self):
        """partial — через минуту, failed — через 15 минут, готово — нет; после предела — только вручную."""
        from tests.stubs import stubbed_import
        with stubbed_import("risk_monitoring.tasks") as t:
            cfg = self._cfg()
            self.assertEqual(t.continuation_delay("partial", 0, cfg), 60)
            self.assertEqual(t.continuation_delay("failed", 1, cfg), 900)
            self.assertIsNone(t.continuation_delay("completed", 0, cfg))
            self.assertIsNone(t.continuation_delay("skip", 0, cfg))
            self.assertIsNone(t.continuation_delay("partial", 3, cfg))
            self.assertIsNone(t.continuation_delay("partial", 0, self._cfg(DAILY_CONTINUE_ENABLED=False)))

    def test_outcome_and_key(self):
        """Итог берётся из pipeline.status сводки; already_done / already_running не продолжаются."""
        from tests.stubs import stubbed_import
        with stubbed_import("risk_monitoring.tasks") as t:
            self.assertEqual(t._outcome({"status": "done", "summary": {"pipeline": {"status": "partial"}}}), "partial")
            self.assertEqual(t._outcome({"status": "already_running"}), "skip")
        self.assertEqual(daily.key_date(20260702), "2026-07-02")

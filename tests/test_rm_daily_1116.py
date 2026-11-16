"""Ежедневный прогон, 16.11.2026: одна дата за раз, заказчики и поставщики — в последнем запуске, предел описания
риска для внешней БД, запуск API без --reload на проде."""
import asyncio
import types
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from configs import settings
from risk_monitoring import daily, passport


def _fakes(unfinished=None, held=None, runs=None):
    """Подмены внутренней БД и HTTP-клиента для ``tasks._daily_run``.

    Args:
        unfinished: Строки ``list_rm_unfinished`` (незавершённые прогоны).
        held: Общее множество занятых блокировок (между запусками).
        runs: Список, куда пишутся даты выполненных прогонов.
    """
    held = set() if held is None else held
    runs = [] if runs is None else runs

    class Repo:
        """Внутренняя БД: сохранённого прогона нет, блокировки в памяти."""

        async def __aenter__(self):
            """Открытие."""
            return self

        async def __aexit__(self, *a):
            """Закрытие."""

        async def get_rm_results(self, env, kind, ids):
            """Сохранённых прогонов нет."""
            return {}

        async def list_rm_unfinished(self, env, kind, hours):
            """Незавершённые прогоны."""
            return list(unfinished or [])

        @asynccontextmanager
        async def advisory_lock(self, name):
            """Как pg_try_advisory_lock."""
            got = name not in held
            held.add(name)
            try:
                yield got
            finally:
                if got:
                    held.discard(name)

    class Mgr:
        """HTTP-клиент."""

        def __init__(self, **kw):
            """Параметры."""

        async def __aenter__(self):
            """Открытие."""
            return self

        async def __aexit__(self, *a):
            """Закрытие."""

    class Run:
        """Прогон, который идёт заметное время."""

        def __init__(self, *a, **kw):
            """Параметры."""

        async def run(self, d):
            """Прогон."""
            runs.append(d)
            await asyncio.sleep(0.05)
            return {"pipeline": {"status": "completed"}}

    mods = {"configs.ai_repo": types.SimpleNamespace(AIRepo=Repo),
            "configs.http_client_manager": types.SimpleNamespace(HTTPClientManager=Mgr)}
    return mods, Run, runs


class OneDateAtATimeTest(unittest.TestCase):
    """Прогоны за разные даты не идут одновременно; сначала доделывается более ранняя дата."""

    def _run(self, calls, unfinished=None, **cfg):
        """Запускает ``_daily_run`` по ``calls`` [(дата, force)] одновременно; возвращает итоги и выполненные даты."""
        from tests.stubs import stubbed_import

        mods, Run, runs = _fakes(unfinished)
        with stubbed_import("risk_monitoring.tasks") as t:
            from risk_monitoring import daily as dm

            patches = [mock.patch.object(settings, k, v) for k, v in
                       {"DAILY_ONE_DATE_AT_A_TIME": True, "DAILY_EARLIER_FIRST": True, **cfg}.items()]
            with mock.patch.dict("sys.modules", mods), mock.patch.object(dm, "DailyRun", Run), \
                    mock.patch.object(t, "ONE_DATE_LOCK_PAUSE", 0.01):
                for p in patches:
                    p.start()
                try:
                    async def all_():
                        """Запуски одновременно."""
                        return await asyncio.gather(*(t._daily_run("stage", d, f, True, True) for d, f in calls))

                    out = asyncio.run(all_())
                finally:
                    for p in patches:
                        p.stop()
        return out, runs

    def test_second_date_postponed(self):
        """Две даты одновременно: вторая откладывается (other_date_running), прогон выполняется один."""
        out, runs = self._run([("2026-11-13", False), ("2026-11-14", False)])
        self.assertEqual(len(runs), 1)
        self.assertEqual(sorted(o["status"] for o in out), ["done", "postponed"])
        post = next(o for o in out if o["status"] == "postponed")
        self.assertEqual(post["reason"], "other_date_running")
        self.assertFalse(post["stored"])

    def test_switch_off(self):
        """DAILY_ONE_DATE_AT_A_TIME = False — даты параллельно, как раньше."""
        out, runs = self._run([("2026-11-13", False), ("2026-11-14", False)], DAILY_ONE_DATE_AT_A_TIME=False)
        self.assertEqual(sorted(runs), ["2026-11-13", "2026-11-14"])

    def test_earlier_unfinished_first(self):
        """Незавершённая более ранняя дата — поздняя откладывается; исчерпавшая попытки не задерживает; force —
        без проверки."""
        rows = [{"external_id": daily.run_key("2026-11-13"), "status": "partial",
                 "ai_analysis": {"date": "2026-11-13", "pipeline": {"attempt": 3}}}]
        out, runs = self._run([("2026-11-14", False)], unfinished=rows)
        self.assertEqual(out[0]["status"], "postponed")
        self.assertEqual(out[0]["reason"], "earlier_unfinished:2026-11-13")
        self.assertEqual(runs, [])

        rows[0]["ai_analysis"]["pipeline"]["attempt"] = settings.DAILY_CONTINUE_MAX_ATTEMPTS
        out, runs = self._run([("2026-11-14", False)], unfinished=rows)
        self.assertEqual(runs, ["2026-11-14"])

        rows[0]["ai_analysis"]["pipeline"]["attempt"] = 1
        out, runs = self._run([("2026-11-14", True)], unfinished=rows)
        self.assertEqual(runs, ["2026-11-14"])

        # более поздняя незавершённая дата не мешает ранней
        out, runs = self._run([("2026-11-12", False)], unfinished=rows)
        self.assertEqual(runs, ["2026-11-12"])


class PostponedContinuationTest(unittest.TestCase):
    """Отложенный запуск: новая дата повторяется задачей, незавершённая — страховкой; номер цепочки не растёт."""

    def test_outcome_and_delay(self):
        """postponed без сохранённого прогона — повтор через DAILY_WAIT_OTHER_DATE_SEC; с сохранённым — skip."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.tasks") as t:
            self.assertEqual(t._outcome({"status": "postponed", "stored": False}), "postponed")
            self.assertEqual(t._outcome({"status": "postponed", "stored": True}), "skip")
            cfg = types.SimpleNamespace(DAILY_CONTINUE_ENABLED=True, DAILY_CONTINUE_DELAY_SEC=60,
                                        DAILY_RETRY_FAILED_DELAY_SEC=900, DAILY_CONTINUE_MAX_ATTEMPTS=3,
                                        DAILY_WAIT_OTHER_DATE_SEC=600)
            # отложенный повтор — и после предела продолжений (это не продолжение)
            self.assertEqual(t.continuation_delay("postponed", 5, cfg), 600)

    def test_chain_kept(self):
        """Повтор отложенного запуска — с тем же номером цепочки и force."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.tasks") as t:
            with mock.patch.object(t.rm_daily_task, "apply_async") as aa:
                t._continue("stage", "2026-11-14", "postponed", 2, True, True, None, force=True)
                t._continue("stage", "2026-11-14", "partial", 2, True, True, None, force=True)
            first, second = aa.call_args_list
            self.assertEqual(first.kwargs["kwargs"]["chain"], 2)
            self.assertTrue(first.kwargs["kwargs"]["force"])
            self.assertEqual(second.kwargs["kwargs"]["chain"], 3)
            self.assertNotIn("force", second.kwargs["kwargs"])


class DeferOrgsTest(unittest.TestCase):
    """Заказчики и поставщики — только в последнем запуске за дату."""

    def _run(self, files="partial", attempt=1):
        """Прогон с состоянием этапов документов и номером запуска."""
        run = daily.DailyRun.__new__(daily.DailyRun)
        run.stages = {"files": {"status": files}, "xml": {"status": "completed"}}
        run.runs = [{}] * (attempt - 1)
        run.stages_on = daily.STAGES
        return run

    def test_deferred_while_docs_partial(self):
        """Документы partial и продолжение будет — отложить; документы готовы / последний запуск / выключено —
        считать."""
        with mock.patch.object(settings, "DAILY_DEFER_ORGS_UNTIL_DOCS_DONE", True), \
                mock.patch.object(settings, "DAILY_CONTINUE_ENABLED", True), \
                mock.patch.object(settings, "DAILY_CONTINUE_MAX_ATTEMPTS", 12):
            self.assertIn("files", self._run().orgs_deferred())
            self.assertIsNone(self._run(files="completed").orgs_deferred())
            self.assertIsNone(self._run(attempt=12).orgs_deferred())
            self.assertIn("запуск 11", self._run(attempt=11).orgs_deferred())
            with mock.patch.object(settings, "DAILY_DEFER_ORGS_UNTIL_DOCS_DONE", False):
                self.assertIsNone(self._run().orgs_deferred())
            with mock.patch.object(settings, "DAILY_CONTINUE_ENABLED", False):
                self.assertIsNone(self._run().orgs_deferred())

    def test_deferred_stage_and_carry(self):
        """Этап — deferred; блоки прошлой сводки переносятся; в фактах — что заказчики пересчитаются позже."""
        run = self._run()
        self.assertEqual(run._deferred("customers", "причина"), [])
        self.assertEqual(run.stages["customers"]["status"], "deferred")
        summary = {"customers": [], "suppliers": [], "stages": run.stages}
        prev = {"customers": [{"name": "Заказчик"}], "suppliers": [],
                "pipeline": {"finished_at": "2026-11-15T08:34:17"}}
        daily.carry_org_blocks(summary, prev)
        self.assertEqual(summary["customers"], [{"name": "Заказчик"}])
        self.assertEqual(summary["carried"]["blocks"], ["customers"])
        facts = daily.summary_facts({**summary, "date": "2026-11-13", "attention": [],
                                     "contracts": {"total": 0, "zones": {}}, "events": {"total": 0},
                                     "scope": {"xml": 1}})
        self.assertTrue(any("пересчитаны после обработки всех документов" in f for f in facts))


class ExternalDescriptionTest(unittest.TestCase):
    """Описание риска для внешней БД не длиннее предела (422 «не может превышать 16000», 12–13.11.2026)."""

    def test_by_findings(self):
        """Несколько находок — целыми находками и строкой «… ещё N»; короткое — без изменений."""
        findings = [{"no": i, "description": "x" * 50} for i in range(1, 11)]
        text = "Находок: 10.\n" + "\n".join(f"{f['no']}. {f['description']}" for f in findings)
        out = passport.shorten_description(text, 250, findings)
        self.assertLessEqual(len(out), 250)
        self.assertTrue(out.startswith("Находок: 10.\n1. "))
        self.assertIn("… ещё", out)
        self.assertEqual(passport.shorten_description("коротко", 250, findings), "коротко")

    def test_plain_cut(self):
        """Без находок — обрезка с «…»."""
        out = passport.shorten_description("слово " * 100, 100)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out.endswith("…"))

    def test_copy_not_mutated(self):
        """Во внешнюю БД — копия; исходный ai_analysis (он же во внутренней БД) не меняется."""
        ai = {"risks": [{"rule_id": "DOC-007", "description": "a" * 300}, {"rule_id": "X", "description": "b"}],
              "pipeline": {"x": 1}}
        out = passport.fit_external_descriptions(ai, 100)
        self.assertLessEqual(len(out["risks"][0]["description"]), 100)
        self.assertEqual(len(ai["risks"][0]["description"]), 300)
        self.assertIs(out["pipeline"], ai["pipeline"])
        self.assertIs(passport.fit_external_descriptions({"risks": [{"description": "b"}]}, 100)["risks"][0]["description"], "b")

    def test_patch_uses_limit(self):
        """patch_ai_analysis отправляет сокращённое описание."""
        from tests.stubs import stubbed_import

        sent = {}

        async def body(self, table, rid, ai):
            """Перехват тела запроса."""
            sent["ai"] = ai
            return {"data": {"id": rid}}

        ai = {"risks": [{"rule_id": "DOC-007", "description": "a" * 300}]}
        with stubbed_import("risk_monitoring.risk_monitoring_api") as m:
            api = m.RiskMonitoringAPI.__new__(m.RiskMonitoringAPI)
            with mock.patch.object(m.RiskMonitoringAPI, "_patch_body", body), \
                    mock.patch.object(m.RiskMonitoringAPI, "_check_table", lambda self, t: None), \
                    mock.patch.object(__import__("configs.settings", fromlist=["x"]), "RM_EXTERNAL_DESCRIPTION_MAX", 100):
                asyncio.run(api.patch_ai_analysis("risk_monitoring_files", 1, ai))
        self.assertLessEqual(len(sent["ai"]["risks"][0]["description"]), 100)
        self.assertEqual(len(ai["risks"][0]["description"]), 300)


class RunApiTest(unittest.TestCase):
    """Запуск API: --reload только вне прода, прежние таймауты сохранены."""

    def test_kwargs(self):
        """reload из настроек; параметры, которых нет у установленного uvicorn, отбрасываются."""
        from scripts.run_api import uvicorn_kwargs

        prod = uvicorn_kwargs(types.SimpleNamespace(API_RELOAD=False))
        self.assertFalse(prod["reload"])
        self.assertEqual((prod["port"], prod["timeout_keep_alive"], prod["timeout_graceful_shutdown"]),
                         (20142, 1200, 600))
        self.assertTrue(uvicorn_kwargs(types.SimpleNamespace(API_RELOAD=True))["reload"])

        def old_run(app, host=None, port=None, reload=False, timeout_keep_alive=5):
            """uvicorn.run без timeout_graceful_shutdown."""

        self.assertNotIn("timeout_graceful_shutdown", uvicorn_kwargs(types.SimpleNamespace(API_RELOAD=False), old_run))

    def test_setting(self):
        """API_RELOAD выключен на проде."""
        self.assertEqual(settings.API_RELOAD, settings.ENVIRONMENT != "prod")


if __name__ == "__main__":
    unittest.main()

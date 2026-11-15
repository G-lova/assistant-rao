"""Ежедневный прогон: этапы закупок, заказчиков и поставщиков ограничены пределом прогона.

Прод, 15.11.2026: прогон за 13.11 снимался лимитом Celery (13 500 с) посреди этапа «suppliers» — сводка не
записывалась, прогон failed и повторялся через 15 минут. Теперь этап, не уложившийся в
``DAILY_STAGES_DEADLINE_SEC``, прерывается (partial), и сводка записывается.
"""
import asyncio
import time
import unittest
from unittest import mock


def run_obj(started_ago: float):
    """``DailyRun`` без внешней БД, начатый ``started_ago`` секунд назад."""
    from risk_monitoring.daily import DailyRun, STAGES

    r = DailyRun.__new__(DailyRun)
    r.stages, r.stages_on = {}, STAGES
    r.started_mono = time.monotonic() - started_ago
    return r


class StageDeadlineTest(unittest.TestCase):
    """``DailyRun._stage``."""

    def setUp(self):
        from configs import settings

        p = mock.patch.object(settings, "DAILY_STAGES_DEADLINE_SEC", 100)
        p.start()
        self.addCleanup(p.stop)

    def test_not_started_after_deadline(self):
        r = run_obj(started_ago=150)
        called = []

        async def stage():
            called.append(1)
            return [1], {"total": 1}

        self.assertIsNone(asyncio.run(r._stage("suppliers", stage)))
        self.assertEqual(called, [])
        self.assertEqual(r.stages["suppliers"]["status"], "partial")

    def test_interrupted_at_deadline(self):
        r = run_obj(started_ago=99.7)

        async def stage():
            await asyncio.sleep(5)
            return [1], {"total": 1}

        t = time.monotonic()
        self.assertIsNone(asyncio.run(r._stage("customers", stage)))
        self.assertLess(time.monotonic() - t, 2)
        self.assertEqual(r.stages["customers"]["status"], "partial")
        self.assertEqual(r.stages["customers"]["reason"], "deadline")

    def test_inner_timeout_is_failure(self):
        r = run_obj(started_ago=0)

        async def stage():
            raise asyncio.TimeoutError()

        with self.assertLogs("risk_monitoring.daily", level="ERROR"):
            self.assertIsNone(asyncio.run(r._stage("contracts", stage)))
        self.assertEqual(r.stages["contracts"]["status"], "failed")

    def test_in_time_and_untimed(self):
        r = run_obj(started_ago=150)   # предел вышел, но files/xml/summary им не ограничены

        async def stage():
            return "ok", {"total": 2}

        self.assertEqual(asyncio.run(r._stage("files", stage)), "ok")
        self.assertEqual(r.stages["files"]["status"], "completed")
        r2 = run_obj(started_ago=0)
        self.assertEqual(asyncio.run(r2._stage("suppliers", stage)), "ok")
        self.assertEqual(r2.stages["suppliers"]["total"], 2)


if __name__ == "__main__":
    unittest.main()

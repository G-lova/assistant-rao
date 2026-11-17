"""Замеры файлов риск-мониторинга (17.11.2026): этапы, запросы к модели, ожидание очереди; сводка прогона."""
import asyncio
import unittest

from configs import llm_resilience as lr
from risk_monitoring import daily, progress


class StepKeyTest(unittest.TestCase):
    """Ключ этапа: одинаковые этапы разных файлов складываются."""

    def test_normalize(self):
        """Скобки и имя модели убираются."""
        self.assertEqual(progress.step_key("анализ рисков: запрос к модели prod_multimodal_llm"), "анализ рисков: модель")
        self.assertEqual(progress.step_key("загрузка прошлой редакции (файл 12)"), "загрузка прошлой редакции")
        self.assertEqual(progress.step_key("чтение текста (.pdf, распознавание сканов)"), "чтение текста")


class CollectTest(unittest.TestCase):
    """Замер этапов и запросов одного объекта, общий для параллельных дочерних задач."""

    def test_steps_and_llm_shared_with_gather(self):
        """Этапы и запросы дочерних задач gather попадают в счётчики объекта; вне замера — не считаются."""
        async def block(i):
            """Блок документа: этап и запрос к модели."""
            async with progress.step(f"анализ рисков: запрос к модели m{i}"):
                lr._count("requests")
                lr._count("llm_sec", 0.5)
                await asyncio.sleep(0)

        async def main():
            """Объект с тремя параллельными блоками."""
            with progress.collect() as tm, lr.scope() as llm:
                await asyncio.gather(*(block(i) for i in range(3)))
                snap = progress.snapshot()
            return tm, llm, snap

        before = lr.CALLS["requests"]
        tm, llm, snap = asyncio.run(main())
        self.assertIn("анализ рисков: модель", tm["steps"])
        self.assertEqual(llm["requests"], 3)
        self.assertEqual(llm["llm_sec"], 1.5)
        self.assertEqual(lr.CALLS["requests"] - before, 3)       # счётчик процесса тоже
        self.assertIn("total_sec", snap)
        self.assertIsNone(progress.snapshot())                    # вне замера
        self.assertIsNone(lr.current_scope())

    def test_gate_wait(self):
        """Ожидание места в очереди к серверу модели — gate_wait_sec."""
        async def main():
            """Два запроса при пределе 1: второй ждёт первого."""
            with lr.scope() as llm:
                sem = asyncio.Semaphore(1)
                orig = lr._gate
                lr._gate = lambda key: sem
                try:
                    async def one():
                        """Запрос, занимающий место 0,05 с."""
                        async with lr._gated("k"):
                            await asyncio.sleep(0.05)

                    await asyncio.gather(one(), one())
                finally:
                    lr._gate = orig
            return llm

        llm = asyncio.run(main())
        self.assertGreaterEqual(llm["gate_wait_sec"], 0.0)
        self.assertLess(llm["gate_wait_sec"], 1.0)


class FileTimingTest(unittest.TestCase):
    """AIFileAnalyzer: pipeline.timings результата и журнал timing_log, в том числе при прерывании пачки."""

    def _fa(self):
        """Анализатор без БД и внешнего API."""
        from tests.stubs import stubbed_import

        ctx = stubbed_import("risk_monitoring.ai_file_analyzer")
        m = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        fa = m.AIFileAnalyzer.__new__(m.AIFileAnalyzer)
        fa.semaphore = asyncio.Semaphore(1)
        fa.force = True
        fa.push_results = False
        return fa, m

    def test_timings_in_result_and_log(self):
        """Готовый файл: этапы и запросы — в pipeline.timings и в timing_log."""
        fa, m = self._fa()

        async def analyze_file(row, previous, attempt):
            """Файл: этап чтения и запрос к модели, затем запись результата."""
            async with m.progress.step("скачивание файла и чтение текста"):
                m.llm_resilience._count("requests")
            return await fa._finalize(int(row["id"]), None, {"pipeline": {"status": "completed"}})

        fa.analyze_file = analyze_file
        res = asyncio.run(fa._guarded({"id": 7, "file_name": "ТЗ.docx"}, None))
        t = res["ai_analysis"]["pipeline"]["timings"]
        self.assertIn("скачивание файла и чтение текста", t["steps"])
        self.assertEqual(t["llm"]["requests"], 1)
        self.assertIn("queue_sec", t)
        log = fa.timing_log
        self.assertEqual((log[0]["file_id"], log[0]["status"], log[0]["llm"]["requests"]), (7, "done", 1))

    def test_interrupted(self):
        """Пачка прервана по времени: файл в журнале со status = interrupted и этапом, на котором встал."""
        fa, m = self._fa()

        async def analyze_file(row, previous, attempt):
            """Файл, который висит на запросе к модели."""
            async with m.progress.step("анализ рисков: запрос к модели x"):
                await asyncio.sleep(10)

        fa.analyze_file = analyze_file

        async def main():
            """Предел пачки 0,05 с."""
            try:
                await asyncio.wait_for(fa._guarded({"id": 8, "file_name": "скан.pdf"}, None), 0.05)
            except asyncio.TimeoutError:
                pass

        asyncio.run(main())
        self.assertEqual(fa.timing_log[0]["status"], "interrupted")
        self.assertIn("анализ рисков: модель", fa.timing_log[0]["steps"])


class SummaryTest(unittest.TestCase):
    """Сводка времени файлов прогона и краткая история этапов запуска."""

    def test_summary(self):
        """P50 / P95, доля ожидания очереди, самые долгие файлы."""
        recs = [{"file_id": i, "name": f"f{i}", "status": "done", "seconds": float(i), "queue_sec": 1.0,
                 "steps": {"анализ рисков: модель": i * 0.8, "чтение текста": 0.1},
                 "llm": {"requests": 4, "llm_sec": 3.0, "gate_wait_sec": 1.0}} for i in range(1, 21)]
        recs.append({"file_id": 99, "name": "скан.pdf", "status": "interrupted", "seconds": 3000.0,
                     "steps": {"чтение текста": 2990.0}, "llm": {"requests": 200}})
        s = daily.file_timings_summary(recs)
        self.assertEqual((s["analyzed"], s["interrupted"]), (21, 1))
        self.assertEqual(s["p50_sec"], 11.0)
        self.assertEqual(s["max_sec"], 3000.0)
        self.assertEqual(s["slowest"][0]["file_id"], 99)
        self.assertEqual(list(s["slowest"][0]["steps"]), ["чтение текста"])
        self.assertEqual(s["llm"]["gate_share"], 0.25)
        self.assertEqual(next(iter(s["steps_sec"])), "чтение текста")
        self.assertEqual(daily.file_timings_summary([])["analyzed"], 0)

    def test_run_brief(self):
        """История запуска: статус и секунды этапов, P95 файлов; пропущенные этапы не пишутся."""
        brief = daily.run_stage_brief({"files": {"status": "partial", "seconds": 9000, "timings": {"p95_sec": 80.0}},
                                       "customers": {"status": "deferred", "seconds": 0},
                                       "xml": {"status": "skipped"}})
        self.assertEqual(brief, {"files": ["partial", 9000], "customers": ["deferred", 0], "files_p95_sec": 80.0})


if __name__ == "__main__":
    unittest.main()

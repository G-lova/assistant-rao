"""Журнал этапов задач риск-мониторинга (``risk_monitoring.progress``)."""
import asyncio
import unittest

from risk_monitoring import progress


class ProgressTest(unittest.TestCase):
    """Субъект у каждой корутины свой; этапы пишут начало, итог и ошибку; активные видны пульсу."""

    def test_steps_and_subjects(self):
        """Две параллельные корутины пишут строки со своими субъектами; ошибка этапа помечается ✗."""
        seen = {}

        async def work(name, fail=False):
            progress.subject(name)
            async with progress.step("поиск похожих документов") as st:
                seen[name] = progress.active()
                await asyncio.sleep(0.01)
                st["detail"] = "найдено 2"
                if fail:
                    raise RuntimeError("сбой")

        async def main():
            return await asyncio.gather(work("Файл 1"), work("Файл 2", fail=True), return_exceptions=True)

        with self.assertLogs("risk_monitoring.progress", level="INFO") as logs:
            res = asyncio.run(main())
        text = "\n".join(logs.output)
        self.assertIn("Файл 1 ▶ поиск похожих документов", text)
        self.assertIn("Файл 1 ✓ поиск похожих документов", text)
        self.assertIn("(найдено 2)", text)
        self.assertIn("Файл 2 ✗ поиск похожих документов", text)
        self.assertIsInstance(res[1], RuntimeError)
        self.assertTrue(any("Файл 1: поиск похожих документов" in a for a in seen["Файл 2"]))
        self.assertEqual(progress.active(), [])


if __name__ == "__main__":
    unittest.main()

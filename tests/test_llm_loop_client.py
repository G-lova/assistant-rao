"""``LoopBoundOpenAI``: свой ``AsyncOpenAI`` на цикл событий.

Прод, 22.11.2026: «Ошибка при определении типа документа …: Event loop is closed» — общий на процесс клиент
(``get_llm`` в ``lru_cache``) держал соединения цикла предыдущей задачи Celery.
"""
import asyncio
import unittest


class LoopBoundTest(unittest.TestCase):
    def setUp(self):
        try:
            from configs.llm_client import LoopBoundOpenAI
        except ImportError as e:   # нет openai в окружении
            self.skipTest(str(e))
        self.cls = LoopBoundOpenAI

    def test_client_per_loop(self):
        c = self.cls(base_url="http://llm.example/v1", api_key="k", timeout=5)

        async def grab():
            return c._client(), c._client(), str(c.base_url)

        a1, a2, url = asyncio.run(grab())   # задача 1
        b1, _, _ = asyncio.run(grab())      # задача 2 — новый цикл
        self.assertIs(a1, a2)               # в одном цикле — один клиент
        self.assertIsNot(a1, b1)            # в новом цикле — новый клиент
        self.assertIn("llm.example", url)
        self.assertEqual(len(c._clients), 1)   # клиенты закрытых циклов отброшены

    def test_with_options_and_resilient(self):
        from configs.llm_resilience import ResilientClient

        c = self.cls(base_url="http://llm.example/v1", api_key="k", timeout=5)
        r = ResilientClient(c)
        self.assertIsInstance(r.raw_client, self.cls)          # повторы SDK выключены, привязка к циклу остаётся
        self.assertEqual(r.raw_client._kwargs["max_retries"], 0)

        async def retries():
            return r.raw_client.max_retries

        self.assertEqual(asyncio.run(retries()), 0)


if __name__ == "__main__":
    unittest.main()

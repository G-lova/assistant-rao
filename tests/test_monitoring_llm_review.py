"""Тесты ИИ-слоя мониторинга: проверка цитат и вызов модели через подставной ``llm``."""

import asyncio
import unittest

from knowledge_store import monitoring_llm_review as ml

DOCS = {"ТЗ.docx": "Требования: автомобили с логотипом, согласованным в ГУ МВД. Объём услуг 24 часа.",
        "КП.pdf": "Коммерческое предложение на физическую невооружённую охрану"}


class FakeLLM:
    """Возвращает заранее заданный ответ модели."""

    def __init__(self, findings):
        self.findings = findings

    async def ask(self, system, user, schema, **kw):
        return {"findings": self.findings}


class MonitoringLLMTest(unittest.TestCase):
    def test_verified_quote_kept(self):
        f = [{"document": "ТЗ.docx", "quote": "автомобили с логотипом, согласованным в ГУ МВД", "explanation": "Избыточное требование"}]
        self.assertEqual(len(asyncio.run(ml.review(FakeLLM(f), "OBJECT", DOCS))), 1)

    def test_invented_quote_dropped(self):
        f = [{"document": "ТЗ.docx", "quote": "исполнитель обязан иметь бронированный автомобиль", "explanation": "Выдумка"}]
        self.assertEqual(asyncio.run(ml.review(FakeLLM(f), "OBJECT", DOCS)), [])

    def test_reasoning_leak_dropped(self):
        f = [{"document": "ТЗ.docx", "quote": "Объём услуг 24 часа", "explanation": "Давайте пересмотрим цитату"}]
        self.assertEqual(asyncio.run(ml.review(FakeLLM(f), "OBJECT", DOCS)), [])

    def test_empty_docs_no_call(self):
        trace = {}
        self.assertEqual(asyncio.run(ml.review(FakeLLM([]), "NMCK", {}, trace)), [])
        self.assertFalse(trace["has_text"])


if __name__ == "__main__":
    unittest.main()

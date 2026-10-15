"""Ограничение параллельности запросов к модели в /evaluate-documents (на каждый вызов, а не на gather)."""
import asyncio
import unittest
from unittest import mock

from tests.stubs import stubbed_import


class Peak:
    """Счётчик одновременно выполняющихся корутин."""

    def __init__(self):
        """Обнуляет счётчики."""
        self.now = 0
        self.peak = 0

    async def work(self, value=None):
        """Имитирует запрос к модели."""
        self.now += 1
        self.peak = max(self.peak, self.now)
        await asyncio.sleep(0.01)
        self.now -= 1
        return value


class ConcurrencyTests(unittest.TestCase):
    """TypeDataExtractor и TasksPipeline соблюдают лимит."""

    def test_extractor_limits_chunks(self):
        """10 блоков при семафоре 2 — не больше двух запросов одновременно, порядок результатов сохраняется."""
        with stubbed_import("evaluate_documents.type_data_extractor") as module:
            ex = module.TypeDataExtractor.__new__(module.TypeDataExtractor)
            peak = Peak()

            async def scenario():
                ex.semaphore = asyncio.Semaphore(2)
                ex.extract_data_from_chunk = lambda content, document_name, doc_type: peak.work({"raw_data": {"c": content}})
                ex.merge_chunk_results = mock.AsyncMock(side_effect=lambda results, *a: {"merged": [r["raw_data"]["c"] for r in results]})
                return await ex.extract_data_from_document([str(i) for i in range(10)], "doc", "t", 6)
            result = asyncio.run(scenario())
            self.assertEqual(peak.peak, 2)
            self.assertEqual(result["merged"], [str(i) for i in range(10)])

    def test_pipeline_limited(self):
        """_limited держит место в семафоре на время каждого вызова."""
        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as module:
            peak = Peak()

            async def scenario():
                sem = asyncio.Semaphore(3)
                return await asyncio.gather(*(module.TasksPipeline._limited(sem, peak.work(i)) for i in range(12)))
            self.assertEqual(asyncio.run(scenario()), list(range(12)))
            self.assertEqual(peak.peak, 3)


if __name__ == "__main__":
    unittest.main()

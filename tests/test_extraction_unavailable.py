"""Извлечение данных файла РМ при недоступной модели: файл failed (повтор позже), а не completed без данных.

Прод, 15.11.2026: «Счет 00026266.pdf» — 504 после всех повторов (924 с), «ни один блок не разобран», и анализ
шёл дальше: файл записывался с пустым извлечением навсегда.
"""
import asyncio
import types
import unittest

from tests.stubs import stubbed_import


class Err504(Exception):
    """Ответ шлюза 504 (как ``openai.InternalServerError``)."""
    status_code = 504


def extractor(module, fail_blocks):
    """``DataExtractor`` с подменённым запросом блока: блоки из ``fail_blocks`` — 504, остальные — данные."""
    ex = module.DataExtractor.__new__(module.DataExtractor)
    ex.semaphore = asyncio.Semaphore(4)
    calls = []

    async def chunk(content, filename, profile=None, hint="", raise_unavailable=False, **kw):
        calls.append(raise_unavailable)
        if content in fail_blocks:
            if raise_unavailable:
                raise module.ModelUnavailable("Err504: 504")
            return None
        return {"readability": {"status": "allow"}, "raw_data": {"summary": content}}

    ex.extract_data_from_chunk = chunk
    return ex, calls


class Stubbed(unittest.TestCase):
    """``evaluate_documents.data_extractor`` с заглушками недостающих пакетов (magic, json_repair …)."""

    def setUp(self):
        ctx = stubbed_import("evaluate_documents.data_extractor")
        self.module = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


class DocumentLevelTest(Stubbed):
    """``extract_data_from_document``."""

    def test_raise_when_requested(self):
        ex, _ = extractor(self.module, {"b"})
        with self.assertRaises(self.module.ModelUnavailable):
            asyncio.run(ex.extract_data_from_document(["a", "b"], "x.pdf", raise_unavailable=True))

    def test_default_keeps_old_behaviour(self):
        ex, calls = extractor(self.module, {"a"})
        res = asyncio.run(ex.extract_data_from_document(["a"], "x.pdf"))
        self.assertEqual(res["readability"]["status"], "deny")
        self.assertEqual(calls, [False])


class EmptyTest(Stubbed):
    """Ни один блок не дал годного ответа (пустой ответ, мусор) — ``ExtractionFailed`` при ``raise_empty``."""

    def test_raise_empty(self):
        ex, _ = extractor(self.module, {"a", "b"})   # без raise_unavailable блоки возвращают None
        orig = ex.extract_data_from_chunk

        async def no_raise(content, filename, **kw):
            kw["raise_unavailable"] = False
            return await orig(content, filename, **kw)

        ex.extract_data_from_chunk = no_raise
        with self.assertRaises(self.module.ExtractionFailed):
            asyncio.run(ex.extract_data_from_document(["a", "b"], "x.pdf", raise_empty=True))
        res = asyncio.run(ex.extract_data_from_document(["a", "b"], "x.pdf"))
        self.assertEqual(res["readability"]["status"], "deny")

    def test_partial_success_kept(self):
        ex, _ = extractor(self.module, {"b"})
        orig = ex.extract_data_from_chunk

        async def no_raise(content, filename, **kw):
            kw["raise_unavailable"] = False
            return await orig(content, filename, **kw)

        ex.extract_data_from_chunk = no_raise

        async def merge(results, filename):
            return results[0]

        ex.merge_chunk_results = merge   # объединение блоков обращается к модели — здесь не нужно
        # один блок разобран — результат возвращается (без ExtractionFailed)
        res = asyncio.run(ex.extract_data_from_document(["a", "b"], "x.pdf", raise_empty=True))
        self.assertIsInstance(res, dict)
        self.assertNotIn("Ошибка анализа документа", (res.get("readability") or {}).get("issues") or [])


class ChunkLevelTest(Stubbed):
    """``extract_data_from_chunk``: 5xx после повторов → ``ModelUnavailable`` только с ``raise_unavailable``."""

    def make(self):
        ex = self.module.DataExtractor.__new__(self.module.DataExtractor)

        class Limiter:
            """Лимитер частоты."""

            async def acquire(self):
                return None

        ex.rate_limiter = Limiter()
        ex.DATA_EXTRACTOR_SCHEMA = '{"type": "object", "properties": {}}'
        ex.model = "m"
        ex.data_extractor_prompt = "prompt"

        class Completions:
            """``client.chat.completions``."""

            async def create(self, **kw):
                raise Err504("Error code: 504")

        ex.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
        return ex

    def test_chunk(self):
        from unittest import mock

        ex = self.make()
        # повторы async_retry — без пауз
        sleeper = mock.patch("asyncio.sleep", mock.AsyncMock(return_value=None))
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.assertIsNone(asyncio.run(ex.extract_data_from_chunk("текст", "x.pdf")))
        with self.assertRaises(self.module.ModelUnavailable):
            asyncio.run(ex.extract_data_from_chunk("текст", "x.pdf", raise_unavailable=True))


class GuardedTest(unittest.TestCase):
    """``AIFileAnalyzer._guarded``: ``ModelUnavailable`` — failed без засчитанной попытки."""

    def test_attempt_not_counted(self):
        from configs.llm_resilience import ModelUnavailable
        from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

        a = AIFileAnalyzer.__new__(AIFileAnalyzer)
        a.force = False
        a.semaphore = asyncio.Semaphore(1)
        a.internal = {}
        failures = []

        async def analyze_file(row, previous, attempt):
            raise ModelUnavailable("Err504: 504")

        async def push_failure(file_id, attempt, error):
            failures.append((attempt, error))
            return {"file_id": file_id, "status": "error"}

        a.analyze_file = analyze_file
        a._push_failure = push_failure
        asyncio.run(a._guarded({"id": 5, "file_name": "Счет.pdf", "rm_status": "failed", "rm_attempt": 1}, None))
        self.assertEqual(failures[0][0], 1)          # попытка 2 не засчитана — записана 1
        self.assertTrue(failures[0][1].startswith("model_unavailable"))


if __name__ == "__main__":
    unittest.main()


class NoRetryTest(unittest.TestCase):
    """``async_retry`` не повторяет ``ModelUnavailable`` (повторы клиента уже исчерпаны)."""

    def test_not_retried(self):
        from configs.llm_resilience import ModelUnavailable, retries_exhausted
        from configs.retry_utils import RetryConfig, async_retry

        calls = []

        @async_retry(RetryConfig(max_retries=4, delay=0))
        async def step():
            calls.append(1)
            raise ModelUnavailable("InternalServerError: Error code: 504")

        self.assertTrue(retries_exhausted(ModelUnavailable("x")))
        with self.assertRaises(ModelUnavailable):
            asyncio.run(step())
        self.assertEqual(calls, [1])


class GuidedFallbackTest(Stubbed):
    """Ответ со схемой (``guided_json``) без JSON-объекта — один повтор блока без схемы в запросе (15.11.2026:
    «{ Register Register …» на большой схеме извлечения)."""

    def make(self, answers):
        ex = self.module.DataExtractor.__new__(self.module.DataExtractor)

        class Limiter:
            """Лимитер частоты."""

            async def acquire(self):
                return None

        ex.rate_limiter = Limiter()
        ex.DATA_EXTRACTOR_SCHEMA = '{"type": "object", "properties": {}}'
        ex.model = "m"
        ex.data_extractor_prompt = "prompt"
        seen = []

        class Completions:
            """``client.chat.completions``: ответы по очереди."""

            async def create(self, **kw):
                seen.append(kw)
                msg = types.SimpleNamespace(content=answers.pop(0))
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

        ex.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
        self.module.repair_json = lambda text: text     # json_repair может быть заглушкой: мусор не «чинится»
        return ex, seen

    def test_fallback_without_schema(self):
        ex, seen = self.make(["{ Register Register Register", '{"readability": {"status": "allow"}, "raw_data": {}}'])
        res = asyncio.run(ex.extract_data_from_chunk("текст", "x.pdf"))
        self.assertEqual(res["readability"]["status"], "allow")
        self.assertIn("guided_json", seen[0]["extra_body"])
        self.assertNotIn("guided_json", seen[1]["extra_body"])
        self.assertEqual(len(seen), 2)

    def test_no_second_fallback(self):
        ex, seen = self.make(["{ Register", "{ Register"])
        self.assertIsNone(asyncio.run(ex.extract_data_from_chunk("текст", "x.pdf")))
        self.assertEqual(len(seen), 2)

    def test_guided_off(self):
        from unittest import mock

        ex, seen = self.make(['{"readability": {"status": "allow"}, "raw_data": {}}'])
        with mock.patch.object(self.module.settings, "EXTRACTION_GUIDED_JSON", False):
            asyncio.run(ex.extract_data_from_chunk("текст", "x.pdf"))
        self.assertNotIn("guided_json", seen[0]["extra_body"])

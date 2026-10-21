"""Переполнение контекста модели (400 «maximum context length»): меньший ответ или деление блока."""
import asyncio
import json
import unittest

from configs import llm_resilience as r

MSG = ("Error code: 400 - {'error': {'message': \"This model's maximum context length is 16384 tokens. However, "
       "you requested 3000 output tokens and your prompt contains at least %d input tokens, for a total of at "
       "least 16385 tokens.\", 'type': 'BadRequestError', 'code': 400}}")


class Overflow(Exception):
    """Ответ сервера 400 о переполнении контекста."""

    status_code = 400

    def __init__(self, used: int):
        """Текст как у vLLM."""
        super().__init__(MSG % used)


class Fake:
    """Клиент: длинный запрос — переполнение, иначе ответ с JSON."""

    def __init__(self, limit_chars: int, room_needed: int = 3000):
        """``limit_chars`` — длиннее не помещается; ``room_needed`` — max_tokens, при котором помещается."""
        self.limit_chars, self.room_needed, self.calls = limit_chars, room_needed, []
        self.chat = type("C", (), {})()
        self.chat.completions = self

    async def create(self, **kw):
        """Имитация ``chat.completions.create``."""
        text = kw["messages"][-1]["content"]
        self.calls.append((len(text), kw.get("max_tokens")))
        if len(text) > self.limit_chars:
            raise Overflow(16000)
        if kw.get("max_tokens", 0) > self.room_needed:
            raise Overflow(13385)
        msg = type("M", (), {"content": json.dumps({"raw_data": {"n": len(text)}})})()
        return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()


class ContextOverflowTest(unittest.TestCase):
    """Обработка переполнения в ResilientClient и в TypeDataExtractor."""

    def test_reduce_output(self):
        """«at least 13385» = 16384 − 3000 + 1 — нижняя граница, длина неизвестна: ответ уменьшается вдвое (1500)."""
        fake = Fake(10 ** 6, room_needed=2967)
        client = r.ResilientClient(fake, retries=1)
        asyncio.run(client.chat.completions.create(model="m", max_tokens=3000, messages=[{"role": "user", "content": "x"}]))
        self.assertEqual([c[1] for c in fake.calls], [3000, 1500])

    def test_known_length(self):
        """Точная длина запроса (не нижняя граница) — ответ уменьшается до остатка контекста."""
        kw = {"max_tokens": 3000}

        class E(Exception):
            status_code = 400

            def __str__(self):
                return ("maximum context length is 16384 tokens. However, you requested 3000 output tokens and your "
                        "prompt contains at least 14000 input tokens")

        self.assertTrue(r.fit_output(E(), kw))
        self.assertEqual(kw["max_tokens"], 16384 - 14000 - r.CONTEXT_MARGIN)

    def test_overflow_raises(self):
        """На ответ остаётся меньше MIN_OUTPUT_TOKENS — ContextOverflow (без пауз и повторов)."""
        client = r.ResilientClient(Fake(5), retries=3)
        with self.assertRaises(r.ContextOverflow):
            asyncio.run(client.chat.completions.create(model="m", max_tokens=3000,
                                                       messages=[{"role": "user", "content": "x" * 50}]))

    def test_split_halves(self):
        """Деление по абзацу ближе к середине."""
        parts = r.split_halves("а" * 300 + "\n\n" + "б" * 300)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[1].startswith("б"))
        self.assertEqual(r.split_halves("коротко"), ["коротко"])

    def test_extractor_splits(self):
        """TypeDataExtractor: блок не помещается — разбирается по частям, результаты идут в объединение."""
        import sys
        import types

        if "json_repair" not in sys.modules:
            try:
                import json_repair  # noqa: F401
            except ImportError:   # в песочнице пакета нет; в тесте он не вызывается
                sys.modules["json_repair"] = types.SimpleNamespace(repair_json=lambda s: s)
        try:
            from evaluate_documents.type_data_extractor import TypeDataExtractor
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        fake = Fake(limit_chars=2600)
        ex = TypeDataExtractor(r.ResilientClient(fake, retries=1), "m")
        ex.rate_limiter = type("L", (), {"acquire": staticmethod(lambda: asyncio.sleep(0))})()
        text = ("строка документа " * 20 + "\n\n") * 8
        res = asyncio.run(ex.extract_data_from_chunk(content=text, document_name="UPD.zip", doc_type="docCargoTaxFiles"))
        self.assertIsInstance(res, list)
        self.assertGreaterEqual(len(res), 2)


if __name__ == "__main__":
    unittest.main()


class AskTextTest(unittest.TestCase):
    """``LLMJson.ask_text``: резюме обычным текстом, <think> и JSON-обёртка убираются."""

    def test_ask_text(self):
        """Ответ с рассуждением и в JSON → чистый текст."""
        try:
            from risk_monitoring.llm_json import LLMJson
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        answers = iter(["<think>думаю</think>Закупка бокса; риски DOC-003.", '{"resume": "Текст резюме."}'])

        class C:
            def __init__(self):
                self.chat = type("X", (), {})()
                self.chat.completions = self

            async def create(self, **kw):
                msg = type("M", (), {"content": next(answers)})()
                return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()

        llm = LLMJson(C(), "m")
        from configs import llm_resilience as lr
        lr.STREAM, saved = False, lr.STREAM
        try:
            self.assertEqual(asyncio.run(llm.ask_text("s", "u")), "Закупка бокса; риски DOC-003.")
            self.assertEqual(asyncio.run(llm.ask_text("s", "u")), "Текст резюме.")
        finally:
            lr.STREAM = saved

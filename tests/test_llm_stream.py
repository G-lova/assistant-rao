"""Ответ модели потоком (``LLM_STREAM_RESPONSES``): шлюз llmhub не обрывает длинную генерацию (504 через 90 с).

``ResilientClient`` запрашивает ``stream=True`` и собирает части в ответ того же вида, что без потока.
"""
import asyncio
import types
import unittest
from unittest import mock


def chunk(content=None, reasoning=None, finish=None, usage=None):
    """Часть потока как у SDK (``ChatCompletionChunk``)."""
    delta = types.SimpleNamespace(content=content, reasoning_content=reasoning)
    return types.SimpleNamespace(id="c1", model="m", usage=usage,
                                 choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])


class Stream:
    """Асинхронный поток частей."""

    def __init__(self, parts):
        self.parts = list(parts)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.parts:
            raise StopAsyncIteration
        return self.parts.pop(0)


def client(result):
    """Клиент SDK, у которого ``create`` возвращает ``result`` и запоминает параметры."""
    seen = []

    async def create(**kw):
        seen.append(kw)
        return result

    raw = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
                                base_url="http://llm-stream-test/")
    return raw, seen


class StreamTest(unittest.TestCase):
    """``ResilientClient`` с ``LLM_STREAM_RESPONSES``."""

    def run_create(self, result, **kw):
        from configs import settings
        from configs.llm_resilience import ResilientClient

        raw, seen = client(result)
        with mock.patch.object(settings, "LLM_STREAM_RESPONSES", True):
            resp = asyncio.run(ResilientClient(raw, retries=1).chat.completions.create(
                model="m", messages=[{"role": "user", "content": "x"}], max_tokens=10, **kw))
        return resp, seen

    def test_assembled(self):
        parts = [chunk(reasoning="думаю"), chunk(content='{"a": '), chunk(content="1}"), chunk(finish="stop"),
                 chunk(usage=types.SimpleNamespace(completion_tokens=3))]
        parts[-1].choices = []
        resp, seen = self.run_create(Stream(parts), extra_body={"guided_json": {"type": "object"}})
        self.assertTrue(seen[0]["stream"])
        self.assertEqual(seen[0]["extra_body"]["guided_json"], {"type": "object"})
        self.assertEqual(resp.choices[0].message.content, '{"a": 1}')
        self.assertEqual(resp.choices[0].message.reasoning_content, "думаю")
        self.assertEqual(resp.choices[0].finish_reason, "stop")
        self.assertEqual(resp.usage.completion_tokens, 3)

    def test_non_stream_passthrough(self):
        plain = types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="ok"), finish_reason="stop")])
        resp, _ = self.run_create(plain)
        self.assertIs(resp, plain)

    def test_disabled(self):
        from configs import settings
        from configs.llm_resilience import ResilientClient

        plain = types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="ok"), finish_reason="stop")])
        raw, seen = client(plain)
        with mock.patch.object(settings, "LLM_STREAM_RESPONSES", False):
            asyncio.run(ResilientClient(raw, retries=1).chat.completions.create(model="m", messages=[]))
        self.assertNotIn("stream", seen[0])


if __name__ == "__main__":
    unittest.main()


class LoopCutTest(unittest.TestCase):
    """Зацикливание в потоке (``LLM_LOOP_CUT``): поток обрывается, запрос повторяется со штрафом за повторы."""

    def make(self, streams):
        """Клиент: каждый вызов ``create`` отдаёт следующий поток; параметры запоминаются."""
        seen = []

        async def create(**kw):
            seen.append(kw)
            return streams.pop(0)

        raw = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
                                    base_url="http://llm-loop-test/")
        return raw, seen

    def run_create(self, streams):
        from configs import settings
        from configs.llm_resilience import ResilientClient

        raw, seen = self.make(streams)
        with mock.patch.multiple(settings, LLM_STREAM_RESPONSES=True, LLM_LOOP_CUT=True):
            resp = asyncio.run(ResilientClient(raw, retries=1).chat.completions.create(
                model="m", messages=[{"role": "user", "content": "x"}], max_tokens=4000))
        return resp, seen

    def test_loop_cut_and_penalized_retry(self):
        looping = Stream([chunk(content="Register ") for _ in range(5000)])
        good = Stream([chunk(content='{"a": 1}'), chunk(finish="stop")])
        resp, seen = self.run_create([looping, good])
        self.assertEqual(len(seen), 2)
        self.assertGreater(len(looping.parts), 4000)          # поток оборван задолго до конца
        self.assertIn("frequency_penalty", seen[1])           # повтор — со штрафом за повторы
        self.assertEqual(resp.choices[0].message.content, '{"a": 1}')

    def test_second_loop_returned(self):
        resp, seen = self.run_create([Stream([chunk(content="Register ") for _ in range(5000)]),
                                      Stream([chunk(content="Register ") for _ in range(5000)])])
        self.assertEqual(len(seen), 2)                        # не больше одного повтора
        self.assertTrue(resp.loop_cut)
        self.assertEqual(resp.choices[0].finish_reason, "length")

    def test_normal_json_not_cut(self):
        import json
        import random

        random.seed(3)
        items = [{"value": random.randint(1000, 10 ** 7) / 100, "context": f"Пункт {i}.{random.randint(1, 9)} "
                  f"цена {random.randint(1000, 99999)} руб., раздел {random.randint(1, 12)}",
                  "evidence": {"fragment": f"стр. {random.randint(1, 40)} приложение {random.randint(1, 5)}",
                               "confidence": 0.9}} for i in range(80)]
        text = json.dumps({"finances": items}, ensure_ascii=False, indent=1)
        parts = [chunk(content=text[i:i + 20]) for i in range(0, len(text), 20)] + [chunk(finish="stop")]
        resp, seen = self.run_create([Stream(parts)])
        self.assertEqual(len(seen), 1)
        self.assertFalse(resp.loop_cut)
        self.assertEqual(resp.choices[0].message.content, text)


class CallLlmLoopCutTest(LoopCutTest):
    """То же в ``call_llm`` (``LLMJson.ask``: анализ рисков, профили, решения РМ) — свой поток в ``_create``.

    15.11.2026 анализ рисков получал «{ductductduct…» по 12 тыс. символов: обрыв был только в
    ``_Completions.create``, а ``call_llm`` идёт мимо него.
    """

    def run_call(self, streams):
        from configs import llm_resilience as r
        from configs import settings

        raw, seen = self.make(streams)
        with mock.patch.object(settings, "LLM_LOOP_CUT", True), mock.patch.object(r, "STREAM", True):
            text = asyncio.run(r.call_llm(raw, retries=1, model="m", max_tokens=4000,
                                          messages=[{"role": "user", "content": "x"}]))
        return text, seen

    def test_call_llm_cut_and_penalized_retry(self):
        looping = Stream([chunk(content="duct") for _ in range(5000)])
        good = Stream([chunk(content='{"a": 1}'), chunk(finish="stop")])
        text, seen = self.run_call([looping, good])
        self.assertEqual(len(seen), 2)
        self.assertGreater(len(looping.parts), 3000)          # оборван на ~4000 символах из 20 000
        self.assertIn("frequency_penalty", seen[1])
        self.assertEqual(text, '{"a": 1}')

    def test_call_llm_second_loop_returned(self):
        text, seen = self.run_call([Stream([chunk(content="duct") for _ in range(5000)]),
                                    Stream([chunk(content="duct") for _ in range(5000)])])
        self.assertEqual(len(seen), 2)                        # не больше одного повтора
        self.assertLess(len(text), 8000)


class FoldSystemTest(unittest.TestCase):
    """Длинный system-промпт → в начало сообщения user (``LLM_SYSTEM_AS_USER_CHARS``)."""

    def fold(self, messages, limit=100):
        from configs import settings
        from configs.llm_resilience import _fold_system

        with mock.patch.object(settings, "LLM_SYSTEM_AS_USER_CHARS", limit):
            return _fold_system(messages)

    def test_long_folded(self):
        msgs = [{"role": "system", "content": "П" * 200}, {"role": "user", "content": "документ"}]
        out = self.fold(msgs)
        self.assertEqual([m["role"] for m in out], ["user"])
        self.assertTrue(out[0]["content"].startswith("П" * 200))
        self.assertTrue(out[0]["content"].endswith("документ"))
        self.assertEqual(msgs[0]["role"], "system")             # исходный список не меняется

    def test_short_kept_and_off(self):
        msgs = [{"role": "system", "content": "коротко"}, {"role": "user", "content": "x"}]
        self.assertIs(self.fold(msgs), msgs)
        long_msgs = [{"role": "system", "content": "П" * 200}, {"role": "user", "content": "x"}]
        self.assertIs(self.fold(long_msgs, limit=0), long_msgs)

    def test_multimodal_parts(self):
        msgs = [{"role": "system", "content": "П" * 200},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:"}}]}]
        out = self.fold(msgs)
        self.assertEqual(out[0]["content"][0], {"type": "text", "text": "П" * 200})
        self.assertEqual(out[0]["content"][1]["type"], "image_url")

    def test_in_request(self):
        from configs import settings
        from configs.llm_resilience import ResilientClient

        plain = types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="ok"), finish_reason="stop")])
        raw, seen = client(plain)
        with mock.patch.multiple(settings, LLM_STREAM_RESPONSES=False, LLM_SYSTEM_AS_USER_CHARS=100):
            asyncio.run(ResilientClient(raw, retries=1).chat.completions.create(
                model="m", messages=[{"role": "system", "content": "П" * 200}, {"role": "user", "content": "x"}]))
        self.assertEqual([m["role"] for m in seen[0]["messages"]], ["user"])


class NoncePrefixTest(unittest.TestCase):
    """Уникальное начало запроса (``LLM_PREFIX_NONCE``) — обход испорченного кэша префиксов сервера."""

    def run_nonce(self, messages, on=True):
        from configs import llm_resilience as r
        from configs import settings

        with mock.patch.object(settings, "LLM_PREFIX_NONCE", on):
            return r._nonce_prefix(messages)

    def test_unique_and_text_kept(self):
        msgs = [{"role": "user", "content": "промпт\n\nдокумент"}]
        a, b = self.run_nonce(msgs), self.run_nonce(msgs)
        self.assertNotEqual(a[0]["content"], b[0]["content"])          # каждый запрос — своё начало
        self.assertTrue(a[0]["content"].startswith("Запрос "))
        self.assertTrue(a[0]["content"].endswith("промпт\n\nдокумент"))
        self.assertEqual(msgs[0]["content"], "промпт\n\nдокумент")      # исходный список не меняется

    def test_parts_and_off(self):
        parts = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:"}}]}]
        out = self.run_nonce(parts)
        self.assertEqual(out[0]["content"][0]["type"], "text")
        self.assertEqual(out[0]["content"][1]["type"], "image_url")
        msgs = [{"role": "user", "content": "x"}]
        self.assertIs(self.run_nonce(msgs, on=False), msgs)

    def test_in_sdk_after_fold(self):
        """В запросе: system перенесён в user, строка — в самом начале."""
        from configs import llm_resilience as r
        from configs import settings

        with mock.patch.multiple(settings, LLM_PREFIX_NONCE=True, LLM_SYSTEM_AS_USER_CHARS=1):
            out = r._sdk({"model": "m", "messages": [{"role": "system", "content": "S"},
                                                     {"role": "user", "content": "U"}]})
        self.assertEqual(len(out["messages"]), 1)
        self.assertRegex(out["messages"][0]["content"], r"^Запрос [0-9a-f]{12}\.\n\nS\n\nU$")

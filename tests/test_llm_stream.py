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

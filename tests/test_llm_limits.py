"""Общие лимиты запросов к модели и сервису эмбеддингов, повторы без перемножения.

* ``SharedLLMRateLimiter`` — один бак на процесс (на цикл событий), работает в разных ``asyncio.run``;
* ``async_retry`` не повторяет ошибку, которую уже повторил ``ResilientClient`` (``llm_retries_exhausted``);
* ``EmbeddingClient``: общий семафор на сервис, деление пакета при 400 без взаимной блокировки.
"""
import asyncio
import sys
import time
import types
import unittest
from unittest import mock


class SharedRateLimiterTest(unittest.TestCase):
    """``configs.rate_limiter.SharedLLMRateLimiter``."""

    def test_shared_between_instances(self):
        """Два экземпляра делят один бак: 4 запроса при 2/с и баке на 2 — не быстрее ~1 с."""
        from configs import rate_limiter, settings

        async def run():
            """Четыре запроса через два разных экземпляра."""
            a, b = rate_limiter.llm_rate_limiter(), rate_limiter.llm_rate_limiter()
            start = time.monotonic()
            for lim in (a, b, a, b):
                await lim.acquire()
            return time.monotonic() - start

        with mock.patch.object(settings, "LLM_RATE_PER_SEC", 2.0, create=True):
            self.assertGreaterEqual(asyncio.run(run()), 0.9)

    def test_new_loop_and_disabled(self):
        """Новый цикл событий получает свой бак (нет «bound to a different event loop»); 0 — без лимита."""
        from configs import rate_limiter, settings

        lim = rate_limiter.llm_rate_limiter()
        with mock.patch.object(settings, "LLM_RATE_PER_SEC", 50.0, create=True):
            asyncio.run(lim.acquire())
            asyncio.run(lim.acquire())
        with mock.patch.object(settings, "LLM_RATE_PER_SEC", 0, create=True):
            async def many():
                """Сто запросов без лимита."""
                start = time.monotonic()
                for _ in range(100):
                    await lim.acquire()
                return time.monotonic() - start

            self.assertLess(asyncio.run(many()), 0.1)


class RetryNotMultipliedTest(unittest.TestCase):
    """Повторы ``ResilientClient`` и ``async_retry`` не перемножаются."""

    def test_async_retry_skips_exhausted(self):
        """Помеченная ошибка не повторяется; обычная — повторяется как раньше."""
        from configs.llm_resilience import mark_exhausted
        from configs.retry_utils import RetryConfig, async_retry

        calls = {"marked": 0, "plain": 0}
        cfg = RetryConfig(max_retries=3, delay=0.0, backoff=1.0, exceptions=(Exception,), log_errors=False)

        @async_retry(cfg)
        async def marked():
            """Сбой сервера, уже повторённый клиентом."""
            calls["marked"] += 1
            raise mark_exhausted(RuntimeError("504"))

        @async_retry(cfg)
        async def plain():
            """Обычная ошибка (например, неразбираемый ответ)."""
            calls["plain"] += 1
            raise ValueError("bad json")

        with self.assertRaises(RuntimeError):
            asyncio.run(marked())
        with self.assertRaises(ValueError):
            asyncio.run(plain())
        self.assertEqual(calls, {"marked": 1, "plain": 4})

    def test_resilient_marks_last_error(self):
        """``ResilientClient`` после исчерпания попыток помечает последнюю ошибку."""
        from configs import llm_resilience
        from configs.llm_resilience import ResilientClient, retries_exhausted

        class Err(Exception):
            """Ответ 503."""
            status_code = 503

        calls = []

        async def create(**kw):
            """Сервер всегда отвечает 503."""
            calls.append(1)
            raise Err("503 Service Unavailable")

        raw = types.SimpleNamespace(base_url="http://llm-test-marks",
                                    chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        client = ResilientClient(raw, retries=2)
        with mock.patch.object(llm_resilience, "RETRY_DELAYS", (0.0,)), \
                mock.patch.object(llm_resilience, "BREAKER_FAILS", 100):
            with self.assertRaises(Err) as ctx:
                asyncio.run(client.chat.completions.create(model="m", messages=[]))
        self.assertTrue(retries_exhausted(ctx.exception))
        self.assertEqual(len(calls), 2)


class FakeResponse:
    """Ответ aiohttp для тестов."""

    def __init__(self, status, data=None):
        self.status, self._data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        """JSON ответа."""
        return self._data

    async def text(self):
        """Текст ответа."""
        return str(self._data)


class EmbeddingLimitsTest(unittest.TestCase):
    """``configs.embedding_client.EmbeddingClient``: общий лимит и деление пакета при 400."""

    def setUp(self):
        """Своя заглушка ``aiohttp`` с ``ClientTimeout`` (настоящего пакета может не быть, а заглушки других
        тестов бывают без этого атрибута)."""
        self._patch = mock.patch.dict(sys.modules, {"aiohttp": types.SimpleNamespace(ClientTimeout=lambda **kw: None)})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def client(self, url, concurrency, inflight):
        """Клиент с фальшивой сессией: пакет больше 1 текста — 400, иначе вектор; считает одновременность."""
        from configs.embedding_client import EmbeddingClient

        def post(api_url, json, headers, timeout):
            """Запрос к «сервису»."""
            texts = json["input"] if isinstance(json["input"], list) else [json["input"]]

            class Ctx(FakeResponse):
                """Учитывает одновременные запросы."""

                async def __aenter__(self):
                    inflight["now"] += 1
                    inflight["max"] = max(inflight["max"], inflight["now"])
                    await asyncio.sleep(0.01)
                    return self

                async def __aexit__(self, *exc):
                    inflight["now"] -= 1
                    return False

            if len(texts) > 1:
                return Ctx(400, "too big")
            return Ctx(200, {"data": [{"index": 0, "embedding": [float(len(texts[0]))]}]})

        http = types.SimpleNamespace(get_session=lambda: types.SimpleNamespace(post=post))
        return EmbeddingClient(url, None, "m", batch_size=4, http_manager=http, concurrency=concurrency)

    def test_split_without_deadlock(self):
        """Лимит 1 и 400 на каждом пакете: половины запрашиваются, взаимной блокировки нет."""
        from configs import settings

        inflight = {"now": 0, "max": 0}
        client = self.client("http://emb-deadlock", 1, inflight)
        with mock.patch.object(settings, "EMBEDDING_CONCURRENCY", 1, create=True):
            vecs = asyncio.run(asyncio.wait_for(client.get_embeddings(["a", "bb", "ccc", "dddd", "e"], strict=True), 5))
        self.assertEqual(vecs.tolist(), [[1.0], [2.0], [3.0], [4.0], [1.0]])
        self.assertEqual(inflight["max"], 1)

    def test_shared_between_clients(self):
        """Два клиента одного сервиса делят лимит процесса."""
        from configs import settings

        inflight = {"now": 0, "max": 0}
        a = self.client("http://emb-shared", 10, inflight)
        b = self.client("http://emb-shared", 10, inflight)

        async def run():
            """Оба клиента одновременно."""
            texts = [f"t{i}" for i in range(12)]
            await asyncio.gather(a.get_embeddings(texts), b.get_embeddings(texts))

        with mock.patch.object(settings, "EMBEDDING_CONCURRENCY", 3, create=True):
            asyncio.run(run())
            asyncio.run(run())   # второй цикл событий — свой семафор, без ошибки привязки к циклу
        self.assertLessEqual(inflight["max"], 3)


if __name__ == "__main__":
    unittest.main()

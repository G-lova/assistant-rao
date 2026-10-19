"""Скачивание через /api/media при 429: Retry-After, пауза на весь хост, ограничение параллельности."""
import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from configs import retry_utils
from configs.host_limiter import HostLimiter, host_of, parse_retry_after
from configs.retry_utils import EISRateLimitError, RetryConfig, async_retry, retry_delay


class ParseRetryAfterTest(unittest.TestCase):
    """Разбор заголовка Retry-After."""

    def test_seconds_and_date(self):
        """Число секунд, HTTP-дата, пустое и мусорное значение."""
        self.assertEqual(parse_retry_after("30"), 30.0)
        self.assertEqual(parse_retry_after(" 5 "), 5.0)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("soon"))
        now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
        header = (now + timedelta(seconds=42)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(parse_retry_after(header, now=now), 42.0, delta=0.01)
        self.assertEqual(parse_retry_after("-3"), 0.0)

    def test_host_of(self):
        """Хост в нижнем регистре."""
        self.assertEqual(host_of("https://Expert.RusAcademEdu.ru/api/media/1"), "expert.rusacademedu.ru")


class RetryDelayTest(unittest.TestCase):
    """Расчёт паузы между попытками."""

    def test_retry_after_is_honoured(self):
        """Пауза не короче Retry-After и не длиннее потолка."""
        cfg = RetryConfig(delay=2.0, backoff=2.0, max_delay=60.0)
        for _ in range(50):
            d = retry_delay(cfg, 1, EISRateLimitError("429", retry_after=30))
            self.assertGreaterEqual(d, 30.0)
            self.assertLessEqual(d, 60.0)
        self.assertEqual(retry_delay(cfg, 1, EISRateLimitError("429", retry_after=600)), 60.0)

    def test_rate_limit_without_header_uses_long_base(self):
        """429 без заголовка — rate_limit_delay с ростом, прочие ошибки — прежний delay."""
        cfg = RetryConfig(delay=2.0, backoff=2.0, rate_limit_delay=10.0, max_delay=60.0)
        with mock.patch.object(retry_utils.random, "uniform", return_value=1.0):
            self.assertEqual(retry_delay(cfg, 1, EISRateLimitError("429")), 10.0)
            self.assertEqual(retry_delay(cfg, 3, EISRateLimitError("429")), 40.0)
            self.assertEqual(retry_delay(cfg, 5, EISRateLimitError("429")), 60.0)
            self.assertEqual(retry_delay(cfg, 2, ConnectionError("x")), 4.0)


class AsyncRetryTest(unittest.TestCase):
    """Декоратор async_retry."""

    def test_sleeps_with_retry_after_even_without_logging(self):
        """Пауза есть и при log_errors=False; после успеха — результат."""
        calls = []
        sleeps = []

        @async_retry(RetryConfig(max_retries=2, delay=0.0, exceptions=(EISRateLimitError,), log_errors=False))
        async def flaky():
            """Два раза 429, потом успех."""
            calls.append(1)
            if len(calls) < 3:
                raise EISRateLimitError("429", retry_after=7)
            return "ok"

        async def fake_sleep(sec):
            """Запоминает паузу вместо ожидания."""
            sleeps.append(sec)

        with mock.patch.object(retry_utils.asyncio, "sleep", fake_sleep):
            self.assertEqual(asyncio.run(flaky()), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(s >= 7 for s in sleeps))

    def test_raises_after_last_attempt_without_extra_sleep(self):
        """После последней попытки — исключение, лишней паузы нет."""
        sleeps = []

        @async_retry(RetryConfig(max_retries=1, delay=0.0, exceptions=(EISRateLimitError,)))
        async def always():
            """Всегда 429."""
            raise EISRateLimitError("429")

        async def fake_sleep(sec):
            """Запоминает паузу."""
            sleeps.append(sec)

        with mock.patch.object(retry_utils.asyncio, "sleep", fake_sleep):
            with self.assertRaises(EISRateLimitError):
                asyncio.run(always())
        self.assertEqual(len(sleeps), 1)


class HostLimiterTest(unittest.TestCase):
    """Ограничитель по хосту."""

    def test_concurrency_per_host(self):
        """К одному хосту — не больше concurrency запросов одновременно, другой хост не ждёт."""
        limiter = HostLimiter(concurrency=1)
        active = {"a": 0, "b": 0}
        peak = {"a": 0, "b": 0}

        async def request(url, key):
            """Имитация запроса."""
            async with limiter.slot(url):
                active[key] += 1
                peak[key] = max(peak[key], active[key])
                await asyncio.sleep(0.01)
                active[key] -= 1

        async def main():
            """Параллельные запросы к двум хостам."""
            await asyncio.gather(*[request(f"https://a.ru/api/media/{i}", "a") for i in range(5)],
                                 *[request(f"https://b.ru/api/media/{i}", "b") for i in range(2)])

        asyncio.run(main())
        self.assertEqual(peak["a"], 1)
        self.assertEqual(peak["b"], 1)

    def test_penalty_blocks_all_requests_to_host(self):
        """После penalize запросы к хосту ждут паузу, к другим — нет; пауза не сокращается."""
        limiter = HostLimiter(concurrency=3)
        limiter.penalize("a.ru", 0.2)
        limiter.penalize("a.ru", 0.05)  # короче действующей — игнорируется
        self.assertGreater(limiter.cooldown_left("a.ru"), 0.1)

        async def timed(url):
            """Время до входа в слот."""
            start = time.monotonic()
            async with limiter.slot(url):
                return time.monotonic() - start

        async def main():
            """Два хоста параллельно."""
            return await asyncio.gather(timed("https://a.ru/x"), timed("https://b.ru/x"))

        waited_a, waited_b = asyncio.run(main())
        self.assertGreaterEqual(waited_a, 0.15)
        self.assertLess(waited_b, 0.05)

    def test_works_across_event_loops(self):
        """Семафор создаётся для каждого loop — повторный asyncio.run не падает."""
        limiter = HostLimiter(concurrency=1)

        async def once():
            """Один запрос."""
            async with limiter.slot("https://a.ru/x") as host:
                return host

        self.assertEqual(asyncio.run(once()), "a.ru")
        self.assertEqual(asyncio.run(once()), "a.ru")


if __name__ == "__main__":
    unittest.main()

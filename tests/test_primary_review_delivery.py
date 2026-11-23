"""Тесты доставки результата оценки первичных экспертов в портал (без сети)."""
import asyncio
import json
import time
import unittest
from unittest import mock

from conclusion import primary_review_delivery as d

UUID = "a0518a67-d734-4992-8c68-5d7e5bd1b53f"
RESULT = {"summary_expert_id": 660, "summary_opinion_id": 9967, "summary_updated_at": "2026-11-20T14:05:00.123Z",
          "is_preliminary": True, "group": "procurement", "shared_differences": [],
          "reviews": [{"expert_id": "3", "suggested_range": 0.0, "weighted_percent": None, "match_percent": 40.0,
                       "comment": None, "categories": None},
                      {"expert_id": 4, "suggested_range": None, "weighted_percent": None, "comment": "x"}]}


class FakeResponse:
    """Ответ приёмника: код и текст тела."""

    def __init__(self, status, text):
        self.status, self._text = status, text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text


class FakeSession:
    """Сессия, отвечающая заданной очередью ответов и запоминающая тела запросов."""

    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def post(self, url, headers=None, data=None, timeout=None):
        self.bodies.append(data)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(*reply)


OK = (200, '{"status":"success"}')


class PayloadTests(unittest.TestCase):
    def test_payload_types_and_time(self):
        """id и expert_id — числа, время версии без «T» и долей секунды, 0 получает процент, null остаётся null."""
        payload = d.build_payload("7243", UUID, RESULT)
        self.assertEqual((payload["id"], payload["request_id"]), (7243, UUID))
        data = payload["data"]
        self.assertEqual(data["summary_updated_at"], "2026-11-20 14:05:00")
        first, second = data["reviews"]
        self.assertEqual((first["expert_id"], first["weighted_percent"], first["comment"], first["categories"]),
                         (3, 40.0, "", []))
        self.assertIsNone(second["suggested_range"])

    def test_request_id_must_be_uuid(self):
        """Без UUID или с неверным UUID запуск отклоняется (422)."""
        for bad in (None, "", "123"):
            with self.assertRaises(d.DeliveryError) as ctx:
                d.validate_request_id(bad)
            self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(d.validate_request_id(UUID.upper()), UUID)


class DeliverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.object(d.settings, "PRIMARY_REVIEW_DELIVERY_RETRY_DELAYS", (0, 0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)
        d._DELIVERIES.clear()

    async def test_retry_sends_same_body_with_same_uuid(self):
        """При 502 и обрыве связи повторяется ровно то же тело; после успеха — state delivered."""
        session = FakeSession([(502, "<html>"), asyncio.TimeoutError(), OK])
        body = json.dumps(d.build_payload(1, UUID, RESULT)).encode()
        state = await d.deliver(session, UUID, body, "stage", time.monotonic())
        self.assertEqual((state["state"], state["attempts"]), ("delivered", 3))
        self.assertEqual(session.bodies, [body, body, body])

    async def test_no_retry_on_client_errors(self):
        """403/404/409/422 повторами не лечатся: одна попытка и причина в статусе."""
        for code in (403, 404, 409, 422):
            session = FakeSession([(code, '{"detail": "причина"}')])
            state = await d.deliver(session, UUID, b"{}", "stage", time.monotonic())
            self.assertEqual((state["state"], state["attempts"], state["http_status"]), ("failed", 1, code))
        self.assertEqual(state["detail"], "причина")

    async def test_html_403_is_reported_without_key(self):
        """HTML от nginx: статус failed, в detail нет тела и ключа."""
        state = await d.deliver(FakeSession([(403, "<html>nginx</html>")]), UUID, b"{}", "stage", time.monotonic())
        self.assertIn("не JSON", state["detail"])
        self.assertNotIn("X-API-Key", json.dumps(d.get_delivery(UUID)))

    async def test_window_stops_retries(self):
        """Когда окно доставки вышло, повторов нет."""
        session = FakeSession([(503, "x")])
        state = await d.deliver(session, UUID, b"{}", "stage", time.monotonic() - 10_000)
        self.assertEqual((state["state"], state["attempts"]), ("failed", 1))

    async def test_start_is_idempotent_and_redelivers_stored_body(self):
        """Повторный запуск с тем же UUID не считает заново; после неудачной доставки повторяется сохранённое тело."""
        calls = []

        async def compute():
            calls.append(1)
            return RESULT

        session = FakeSession([(403, "<html>"), OK])
        self.assertEqual(d.start(compute, session, 7243, UUID, "stage")["state"], "calculating")
        while d.get_delivery(UUID)["state"] in ("calculating", "delivering"):
            await asyncio.sleep(0)
        self.assertEqual(d.get_delivery(UUID)["state"], "failed")
        self.assertEqual(d.start(compute, session, 7243, UUID, "stage")["state"], "delivering")
        while d.get_delivery(UUID)["state"] == "delivering":
            await asyncio.sleep(0)
        self.assertEqual((d.get_delivery(UUID)["state"], len(calls)), ("delivered", 1))
        self.assertEqual(session.bodies[0], session.bodies[1])
        self.assertEqual(d.start(compute, session, 7243, UUID, "stage")["state"], "delivered")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

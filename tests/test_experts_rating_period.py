"""Тесты периода расчёта рейтинга экспертов (POST /get-experts-rating, флаг by_period)."""
import unittest
from datetime import datetime
from unittest import mock

from tests.stubs import stubbed_import


class RatingPeriodTest(unittest.TestCase):
    """src.rating.resolve_rating_period."""

    def resolve(self, *args, **kwargs):
        """Вызывает resolve_rating_period (модуль импортируется с заглушками зависимостей)."""
        with stubbed_import("src.rating") as module:
            return module.resolve_rating_period(*args, **kwargs)

    def test_default(self):
        """Флаг выключен: с 01.01.2025 по завтрашнюю дату (весь текущий день), даты запроса не учитываются."""
        now = datetime(2026, 11, 3, 16, 24, 5)
        self.assertEqual(self.resolve(False, None, None, now=now), ("2025-01-01", "2026-11-04"))
        self.assertEqual(self.resolve(False, "2026-01-01", "2026-02-01", now=now), ("2025-01-01", "2026-11-04"))
        self.assertEqual(self.resolve(False, None, None, now=datetime(2026, 12, 31, 23, 59)), ("2025-01-01", "2027-01-01"))

    def test_default_now_moscow(self):
        """Без now — завтрашняя дата в часовом поясе настроек."""
        start, end = self.resolve(False, None, None)
        self.assertEqual(start, "2025-01-01")
        self.assertRegex(end, r"^\d{4}-\d{2}-\d{2}$")

    def test_by_period(self):
        """Флаг включён: даты запроса как есть."""
        self.assertEqual(self.resolve(True, "2026-01-01", "2026-03-31"), ("2026-01-01", "2026-03-31"))
        self.assertEqual(self.resolve(True, " 2026-01-01 ", "2026-03-31 23:59:59"), ("2026-01-01", "2026-03-31 23:59:59"))

    def test_by_period_swap(self):
        """Начало позже конца — даты меняются местами (в т. ч. в пределах одного дня по времени)."""
        self.assertEqual(self.resolve(True, "2026-04-01", "2026-03-31"), ("2026-03-31", "2026-04-01"))
        self.assertEqual(self.resolve(True, "2026-03-31 18:00", "2026-03-31"), ("2026-03-31", "2026-03-31 18:00"))
        self.assertEqual(self.resolve(True, "2026-03-31", "2026-03-31"), ("2026-03-31", "2026-03-31"))

    def test_by_period_errors(self):
        """Флаг включён: без дат, неверный формат, несуществующая дата — ошибка."""
        for args in ((None, "2026-03-31"), ("2026-01-01", ""), ("01.01.2026", "2026-03-31"),
                     ("2026-02-30", "2026-03-31")):
            with self.assertRaises(ValueError, msg=str(args)):
                self.resolve(True, *args)


class RatingRequestTest(unittest.TestCase):
    """Модель запроса: флаг по умолчанию выключен."""

    def test_schema(self):
        """Пустое тело — by_period = false, даты не обязательны."""
        with stubbed_import("configs.schemas") as module:
            r = module.ExpertsRatingRequest()
            self.assertFalse(r.by_period)
            r = module.ExpertsRatingRequest(by_period=True, start_date="2026-01-01", end_date="2026-03-31")
            self.assertTrue(r.by_period)


if __name__ == "__main__":
    unittest.main()

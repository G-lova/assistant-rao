"""Тесты подстановки начала периода рейтинга в SQL подбора экспертов (search_experts.pipeline.apply_rating_start)."""
import os
import unittest

from tests.stubs import stubbed_import

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RatingStartTest(unittest.TestCase):
    """Показатели и критерии экспертов при подборе считаются с EXPERT_RATING_DEFAULT_START, а не за последний год."""

    def setUp(self):
        """Текст запроса подбора."""
        with open(os.path.join(ROOT, "queries", "experts_for_expertise.sql"), encoding="utf-8") as f:
            self.sql = f.read()

    def test_no_year_window_left(self):
        """В запросе не осталось годового окна, плейсхолдер — в 7 местах (показатели и критерии 1–5)."""
        self.assertNotIn("INTERVAL 1 YEAR", self.sql)
        self.assertEqual(self.sql.count("'{{RATING_START}}'"), 7)

    def test_apply(self):
        """Подставляется дата из настроек; число привязок «?» не меняется; кривая дата — ошибка."""
        with stubbed_import("search_experts.pipeline") as m:
            out = m.apply_rating_start(self.sql)
            self.assertNotIn("{{RATING_START}}", out)
            self.assertIn("'2025-01-01'", out)
            self.assertEqual(out.count("?"), self.sql.count("?"))
            self.assertIn("'2026-01-01'", m.apply_rating_start(self.sql, "2026-01-01"))
            with self.assertRaises(ValueError):
                m.apply_rating_start(self.sql, "2025-01-01' OR 1=1 --")


if __name__ == "__main__":
    unittest.main()

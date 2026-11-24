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
        """В запросе не осталось годового окна; плейсхолдер — в 8 местах.

        6 — показатели и критерии в CTE experts, 2 — в CTE declines_since_start (объект и массив declineExperts).
        """
        self.assertNotIn("INTERVAL 1 YEAR", self.sql)
        self.assertEqual(self.sql.count("'{{RATING_START}}'"), 8)

    def test_declines_counted_from_expertises(self):
        """expert_declines берётся из отдельного CTE по declineExperts, а не из соединения с expertise_experts.

        Раньше SUM(... NOT EXISTS(expertise_experts ...)) шёл по строкам, где запись эксперта уже есть,
        поэтому счётчик всегда был 0 (19.11.2026). Удалённые экспертизы для рейтинга не учитываются.
        """
        start = self.sql.index("declines_since_start AS (")
        cte = self.sql[start:self.sql.index("blocked_experts AS (", start)]
        self.assertEqual(cte.count("e.deleted_at IS NULL"), 2)
        self.assertIn("JSON_TYPE(e.declineExperts) = 'OBJECT'", cte)
        self.assertIn("JSON_TYPE(e.declineExperts) = 'ARRAY'", cte)
        self.assertIn("NOT EXISTS", cte)
        self.assertIn("FROM declines_since_start ds", self.sql)
        self.assertNotIn("SUM(CASE WHEN e.dateStatus2", self.sql)

    def test_expert_requests_without_status(self):
        """Заявки эксперта (expert_requests) считаются без условия e1.status = 2."""
        self.assertNotIn("e1.status = 2", self.sql)
        self.assertIn("AND ee1.group_formed_at IS NULL AND ee1.deleted_at IS NULL AND e1.deleted_at IS NULL", self.sql)

    def test_week_workload_in_work_during_week(self):
        """currentWeekWorkload — экспертизы, бывшие в работе у эксперта за последние 7 дней (24.11.2026).

        Группа сформирована; строка эксперта не удалена или снята за последние 7 дней (эксперт успел
        поработать); заключение ещё не загружено или загружено за последние 7 дней. Статус экспертизы
        (e.status) больше не учитывается.
        """
        start = self.sql.index("COUNT(CASE\n            WHEN ee.group_formed_at IS NOT NULL")
        block = self.sql[start:self.sql.index("AS currentWeekWorkload", start)]
        self.assertIn("(ee.deleted_at IS NULL OR ee.deleted_at >= DATE_SUB(NOW(), INTERVAL 7 DAY))", block)
        self.assertIn("(ee.uploadExpertDate IS NULL OR ee.uploadExpertDate >= DATE_SUB(NOW(), INTERVAL 7 DAY))", block)
        self.assertNotIn("e.status IN (3)", self.sql)

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

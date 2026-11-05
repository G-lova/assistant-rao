"""Тесты журнала подбора экспертов: контекст запроса, итог по этапам, краткие списки."""
import unittest
from unittest import mock

import pandas as pd

from tests.stubs import stubbed_import


class LoggingTest(unittest.TestCase):
    """Строки журнала ScoringPipeline и src.scoring."""

    def test_brief(self):
        """Длинный список — число, первые ID по возрастанию и остаток."""
        with stubbed_import("search_experts.pipeline") as m:
            self.assertEqual(m.brief({5, 3, 9}), "3: [3, 5, 9]")
            self.assertEqual(m.brief(range(20), limit=3), "20: [0, 1, 2] (+17)")

    def test_conflicts_and_summary(self):
        """Конфликты — одна строка INFO с причинами, подробности — DEBUG; итог содержит время и отсев."""
        with stubbed_import("search_experts.pipeline") as m:
            p = m.ScoringPipeline.__new__(m.ScoringPipeline)
            p.log_ctx, p.timings, p.funnel = "[подбор 7228/stage]", {"sql": 7.97, "preprocess": 0.0, "similarity": 17.15}, {"кандидатов": 369}
            p.conflict_detector = mock.Mock()
            p.conflict_detector.find_requisite_conflicts.return_value = {1: ["inn_work"], 2: ["guess_inn_work", "name_executor"]}
            p.conflict_detector.find_family_conflicts.return_value = {2, 3}
            p.coauthor_publications = {}
            df = pd.DataFrame({"expert_id": [1, 2, 3, 4], "expert_name": ["а б", "в г", "д е", "ж з"],
                               "expertise_name": ["и к"] * 4})
            with self.assertLogs(m.logger, level="DEBUG") as logs:
                out = p.detect_conflicts(df)
            self.assertEqual(out["expert_id"].tolist(), [4])
            info = [r.getMessage() for r in logs.records if r.levelname == "INFO"]
            self.assertEqual(len(info), 1)
            self.assertIn("[подбор 7228/stage] конфликт интересов: исключено 3: [1, 2, 3]", info[0])
            self.assertIn("family 2", info[0])
            self.assertTrue(any(r.levelname == "DEBUG" for r in logs.records))
            p.funnel.update({"семейственность": 29, "в ответе": 336})
            s = p.summary()
            self.assertTrue(s.startswith("25.12 с (sql 7.97, similarity 17.15) | кандидатов 369 → конфликт −3"), s)
            self.assertIn("семейственность −29 → в ответе 336", s)

    def test_top_preview(self):
        """Первые эксперты ответа: с баллом при details, иначе — ID; пустой ответ."""
        with stubbed_import("src.scoring") as m:
            self.assertEqual(m.top_preview([{425: {"total_rate": "80.08"}}, {523: {"total_rate": "78.38"}}]),
                             "425 (80.08), 523 (78.38)")
            self.assertEqual(m.top_preview(list(range(12)), limit=2), "0, 1 … всего 12")
            self.assertEqual(m.top_preview([]), "нет экспертов")


if __name__ == "__main__":
    unittest.main()

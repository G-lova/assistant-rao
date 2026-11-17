"""Тесты оценки первичных экспертов по заключениям экспертизы (без БД и сети)."""
import json
import unittest

from conclusion.primary_review_pipeline import PrimaryReviewError, review_expertise, split_opinions

SUMMARY = {"field2_1_1": 1, "field2_1_2": 0, "field2_1_3": 1, "field2_1_4": 1, "field2_1_5": 1, "field2_1_6": 1,
           "documents": "Извещение, проект контракта",
           "field3": "1.2. Адрес заказчика в извещении не указан, что нарушает требования закона.",
           "field4": "На основании представленной документации закупку осуществлять не рекомендуется."}


def row(expert_id, data, assignment=1, updated="2026-10-01"):
    """Строка результата SQL: ``assignment`` пуст у сводного эксперта."""
    return {"id": expert_id, "expert_id": expert_id, "data": json.dumps(data), "updated_at": updated,
            "assignment_id": assignment, "current_range": None, "accept": None,
            "object": 6, "type_orig": 4, "checkType2": 1}


class PrimaryReviewPipelineTests(unittest.TestCase):
    """Деление ролей и результат по каждому первичному эксперту."""

    def test_split_roles_and_latest_summary(self):
        """Без назначения — сводный; из нескольких берётся последний по updated_at; NaN из DataFrame — тоже «нет назначения»."""
        rows = [row(1, SUMMARY), row(9, SUMMARY, assignment=float("nan"), updated="2026-10-01"),
                row(10, SUMMARY, assignment=None, updated="2026-10-05")]
        parts = split_opinions(rows)
        self.assertEqual([r["expert_id"] for r in parts["primary"]], [1])
        self.assertEqual(parts["summary"]["expert_id"], 10)

    def test_review_reports_differences_and_comment(self):
        """Пропущенное первичным нарушение попадает в расхождения и комментарий; метка берётся из справочника."""
        first = {**SUMMARY, "field2_1_2": 1}
        rows = [row(1, first), row(2, SUMMARY), row(99, SUMMARY, assignment=None)]
        result = review_expertise(rows, {"field2_1_2": "1.2. Наличие адреса"})
        self.assertEqual(result["summary_expert_id"], 99)
        by_expert = {r["expert_id"]: r for r in result["reviews"]}
        self.assertEqual(by_expert[1]["differences"][0]["kind"], "missed")
        self.assertIn("критерии: 1.2)", by_expert[1]["comment"])
        self.assertEqual(by_expert[2]["differences"], [])
        self.assertEqual(by_expert[2]["comment"], "ЭЗ соответствует требованиям")
        self.assertEqual(by_expert[2]["suggested_range"], 1.0)

    def test_errors(self):
        """Нет первичных — 404; нет сводного — 409."""
        with self.assertRaises(PrimaryReviewError) as no_primary:
            review_expertise([row(99, SUMMARY, assignment=None)])
        self.assertEqual(no_primary.exception.http_status, 404)
        with self.assertRaises(PrimaryReviewError) as no_summary:
            review_expertise([row(1, SUMMARY)])
        self.assertEqual(no_summary.exception.http_status, 409)


if __name__ == "__main__":
    unittest.main()

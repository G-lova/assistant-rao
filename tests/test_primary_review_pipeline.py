"""Тесты оценки первичных экспертов по заключениям экспертизы (без БД и сети)."""
import json
import unittest

from conclusion.primary_review_pipeline import PrimaryReviewError, review_expertise, split_opinions

SUMMARY = {"field2_1_1": 1, "field2_1_2": 0, "field2_1_3": 1, "field2_1_4": 1, "field2_1_5": 1, "field2_1_6": 1,
           "documents": "Извещение, проект контракта",
           "field3": "1.2. Адрес заказчика в извещении не указан, что нарушает требования закона.",
           "field4": "На основании представленной документации закупку осуществлять не рекомендуется."}


def row(expert_id, data, role="expert", updated="2026-10-01"):
    """Строка результата SQL: ``role`` — ``users.role`` автора (``expert`` первичный, ``raoExpert`` сводный)."""
    return {"id": expert_id, "expert_id": expert_id, "data": json.dumps(data), "updated_at": updated,
            "expert_role": role, "current_range": None, "accept": None,
            "object": 6, "type_orig": 4, "checkType2": 1}


class PrimaryReviewPipelineTests(unittest.TestCase):
    """Деление ролей и результат по каждому первичному эксперту."""

    def test_split_roles_and_latest_summary(self):
        """Роль берётся из users.role; из нескольких сводных берётся последний по updated_at; прочие роли игнорируются."""
        rows = [row(1, SUMMARY), row(9, SUMMARY, "raoExpert", updated="2026-10-01"),
                row(10, SUMMARY, "raoExpert", updated="2026-10-05"), row(11, SUMMARY, "admin")]
        parts = split_opinions(rows)
        self.assertEqual([r["expert_id"] for r in parts["primary"]], [1])
        self.assertEqual(parts["summary"]["expert_id"], 10)

    def test_review_reports_differences_and_comment(self):
        """Пропущенное первичным нарушение попадает в расхождения и комментарий; метка берётся из справочника."""
        first = {**SUMMARY, "field2_1_2": 1}
        rows = [row(1, first), row(2, SUMMARY), row(99, SUMMARY, role="raoExpert")]
        result = review_expertise(rows, {"field2_1_2": "1.2. Наличие адреса"})
        self.assertEqual(result["summary_expert_id"], 99)
        by_expert = {r["expert_id"]: r for r in result["reviews"]}
        self.assertEqual(by_expert[1]["differences"][0]["kind"], "missed")
        self.assertIn("по критерию 1.2.", by_expert[1]["comment_details"])   # полный перечень; comment зависит от оценки
        self.assertEqual(by_expert[2]["differences"], [])
        self.assertEqual(by_expert[2]["comment"], "ЭЗ соответствует требованиям")
        self.assertIn(by_expert[2]["suggested_range"], (0.0, 0.5, 0.75, 1.0))
        self.assertEqual(by_expert[2]["range_by_thresholds"], 1.0)
        self.assertIn(by_expert[2]["range_source"], ("weighted_percent", "model", "thresholds"))
        if by_expert[2]["range_source"] == "weighted_percent":      # основной способ: процент и вычеты в ответе
            self.assertEqual(by_expert[2]["weighted_percent"], 100.0)
            self.assertEqual(by_expert[2]["deductions"], [])
            self.assertEqual(by_expert[1]["deductions"][0]["key"], "field2_1_2")
            self.assertLess(by_expert[1]["weighted_percent"], 100.0)
        self.assertLessEqual(by_expert[1]["suggested_range"], 0.75)     # есть расхождение → по порядку не выше 0.75
        self.assertTrue(by_expert[1]["rule_applied"] or by_expert[1]["model_range"] < 1.0)
        self.assertEqual((by_expert[2]["match_percent"], by_expert[2]["range_by_scale"]), (100.0, 1.0))
        self.assertEqual(by_expert[1]["match_percent"], 83.3)     # 1 расхождение из 6 общих критериев

    def test_review_has_categories(self):
        """В ответе есть категории замечаний (общие расхождения не считаются)."""
        rows = [row(1, {**SUMMARY, "field2_1_2": 1}), row(2, SUMMARY), row(99, SUMMARY, role="raoExpert")]
        by_expert = {r["expert_id"]: r for r in review_expertise(rows)["reviews"]}
        first = by_expert[1]
        self.assertIn("MISSED_VIOLATION", [c["code"] for c in first["categories"]])   # плюс дефекты шаблонных текстов
        self.assertNotIn("MISSED_VIOLATION", [c["code"] for c in by_expert[2]["categories"]])
        self.assertNotIn("substantial_risk", first)

    def test_shared_difference_is_not_blamed(self):
        """Расхождение, одинаковое у всех первичных, выделяется и не попадает в комментарий; у одного эксперта — остаётся."""
        both = {**SUMMARY, "field2_1_3": 0}             # итоговое: 1, оба первичных: 0
        rows = [row(1, {**both, "field2_1_5": 0}), row(2, both), row(99, SUMMARY, role="raoExpert")]
        result = review_expertise(rows, {"field2_1_3": "1.3. Наличие почты", "field2_1_5": "1.5. Наличие сайта"})
        self.assertEqual([d["key"] for d in result["shared_differences"]], ["field2_1_3"])
        first, second = result["reviews"]
        self.assertTrue(all(d["shared"] == (d["key"] == "field2_1_3") for d in first["differences"]))
        self.assertIn("1.5", first["comment_details"])
        self.assertNotIn("1.3", first["comment_details"])
        self.assertEqual(second["comment"], "ЭЗ соответствует требованиям")
        self.assertEqual(second["comment_details"], "ЭЗ соответствует требованиям")

    def test_errors(self):
        """Нет первичных — 404; нет сводного — 409."""
        with self.assertRaises(PrimaryReviewError) as no_primary:
            review_expertise([row(99, SUMMARY, role="raoExpert")])
        self.assertEqual(no_primary.exception.http_status, 404)
        with self.assertRaises(PrimaryReviewError) as no_summary:
            review_expertise([row(1, SUMMARY)])
        self.assertEqual(no_summary.exception.http_status, 409)


if __name__ == "__main__":
    unittest.main()

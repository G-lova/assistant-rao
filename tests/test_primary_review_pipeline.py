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
        self.assertNotRegex(by_expert[1]["comment"], r"(?m)^\d+\. ")     # без нумерации
        if by_expert[1]["suggested_range"] == 1.0:                     # при 1.00 comment содержит полный перечень расхождений
            self.assertEqual(by_expert[1]["comment"], by_expert[1]["comment_details"])
        self.assertEqual(by_expert[1]["match_percent"], 83.3)     # 1 расхождение из 6 общих критериев

    def test_review_has_categories(self):
        """В ответе есть категории замечаний (общие расхождения не считаются)."""
        rows = [row(1, {**SUMMARY, "field2_1_2": 1}), row(2, SUMMARY), row(99, SUMMARY, role="raoExpert")]
        by_expert = {r["expert_id"]: r for r in review_expertise(rows)["reviews"]}
        first = by_expert[1]
        self.assertIn("MISSED_VIOLATION", [c["code"] for c in first["categories"]])   # плюс дефекты шаблонных текстов
        self.assertNotIn("MISSED_VIOLATION", [c["code"] for c in by_expert[2]["categories"]])
        self.assertNotIn("substantial_risk", first)

    def test_issue_penalty_fields(self):
        """В ответе есть поля штрафа; без серьёзных замечаний вычета нет, прежняя оценка равна итоговой."""
        by_expert = {r["expert_id"]: r for r in review_expertise(
            [row(1, SUMMARY), row(99, SUMMARY, role="raoExpert")])["reviews"]}
        first = by_expert[1]
        self.assertEqual((first["issue_penalty_steps"], first["range_before_issues"]), (0, first["suggested_range"]))
        self.assertGreaterEqual(first["issue_penalty_percent"], 0.0)
        if first["issue_penalty_steps"]:
            self.assertEqual(first["alternative_range"], first["range_before_issues"])

    def test_report_summary_without_items_list(self):
        """Сводное заключение объекта 7 без ``field1_3`` всё равно распознаётся как отчётное и сравнивается с первичным."""
        flags = {"field3_0": False, "field3_1": False, "field4_0": False, "field4_1": False}
        primary = {**flags, "field1_3": [{"name": "товар"}], "field3_0": True}
        summary = dict(flags)                                   # как в экспертизе 6490: списка field1_3 нет
        rows = [{**row(1, primary), "object": 7, "checkType2": 14}, {**row(99, summary, role="raoExpert"), "object": 7, "checkType2": 14}]
        result = review_expertise(rows)
        self.assertEqual(result["group"], "report")
        self.assertEqual(result["reviews"][0]["common"], 4)
        self.assertEqual([d["key"] for d in result["reviews"][0]["differences"]], ["field3_0"])

    def test_range_reason_when_too_few_criteria(self):
        """Мало общих критериев: оценки нет, причина — в ``range_reason`` и в комментарии (а не «ЭЗ соответствует»)."""
        rows = [row(1, {"field2_1_1": 1}), row(99, {"field2_1_1": 1}, role="raoExpert")]
        review = review_expertise(rows)["reviews"][0]
        self.assertIsNone(review["suggested_range"])
        self.assertIn("не рассчитана", review["range_reason"])
        self.assertTrue(review["comment"].startswith(review["range_reason"]))

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


class SummaryVersionTests(unittest.TestCase):
    """Выбор версии итогового заключения и пометка предварительного расчёта."""

    def _rows(self):
        old = {**SUMMARY, "field2_1_2": 1}
        return [row(1, SUMMARY),
                {**row(9, old, "raoExpert", updated="2026-10-01"), "id": 901},
                {**row(9, SUMMARY, "raoExpert", updated="2026-10-05"), "id": 905}]

    def test_default_is_latest_and_preliminary(self):
        """Без summary_opinion_id берётся последняя версия, расчёт предварительный."""
        result = review_expertise(self._rows())
        self.assertEqual(result["summary_opinion_id"], 905)
        self.assertEqual(result["summary_updated_at"], "2026-10-05")
        self.assertTrue(result["is_preliminary"])

    def test_explicit_version_is_used(self):
        """Указанная версия сравнивается вместо последней и не считается предварительной."""
        result = review_expertise(self._rows(), summary_opinion_id=901)
        self.assertEqual(result["summary_opinion_id"], 901)
        self.assertFalse(result["is_preliminary"])
        self.assertEqual(result["reviews"][0]["suggested_range"], 1.0)    # первичный совпал со старой версией

    def test_unknown_version_is_404(self):
        """Версии нет среди сводных заключений экспертизы — 404."""
        with self.assertRaises(PrimaryReviewError) as ctx:
            review_expertise(self._rows(), summary_opinion_id=12345)
        self.assertEqual(ctx.exception.http_status, 404)

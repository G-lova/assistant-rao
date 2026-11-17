"""Тесты сравнения первичного заключения с итоговым (knowledge_store.primary_compare)."""
import unittest

from knowledge_store import primary_compare as pc, primary_review

SUMMARY = {"field2_1_1": 1, "field2_1_2": 0, "field2_1_3": 1, "field2_1_4": 2, "field2_1_5": 1, "field2_1_6": 1}


class CompareTests(unittest.TestCase):
    """Расхождения, комментарий и предложение оценки."""

    def test_kinds_and_order(self):
        """Пропущенное нарушение идёт первым, затем ложное нарушение, затем 1 против 2; булевы значения приводятся к 1/0."""
        primary = {"field2_1_1": 0, "field2_1_2": True, "field2_1_3": 1, "field2_1_4": 1, "field2_1_5": 1, "field2_1_6": False}
        cmp = pc.compare(primary, SUMMARY)
        self.assertEqual(cmp.common, 6)
        self.assertEqual([(d.key, d.kind) for d in cmp.differences],
                         [("field2_1_2", "missed"), ("field2_1_1", "false_alarm"), ("field2_1_6", "false_alarm"),
                          ("field2_1_4", "other")])

    def test_comment_and_positive(self):
        """Комментарий называет критерий по названию; без расхождений — шаблонная фраза."""
        cmp = pc.compare({**SUMMARY, "field2_1_2": 1}, SUMMARY)
        text = pc.build_comment(cmp, {"field2_1_2": "1.2. Наличие адреса"})
        self.assertIn("1.2. Наличие адреса", text)
        self.assertIn("не выявлено нарушение", text)
        self.assertEqual(pc.build_comment(pc.compare(SUMMARY, SUMMARY)), pc.POSITIVE_COMMENT)

    def test_suggest_range_thresholds(self):
        """Оценка по числу расхождений; при малом числе общих критериев — None."""
        cmp = pc.Comparison(common=50, differences=[pc.Difference(f"field2_1_{i}", 1, 0) for i in range(8)])
        self.assertEqual(pc.suggest_range(cmp, (7, 10, 22)), 0.75)
        self.assertEqual(pc.suggest_range(pc.Comparison(common=2), (7, 10, 22)), None)
        self.assertEqual(pc.suggest_range(pc.Comparison(common=50), (7, 10, 22)), 1.0)

    def test_report_form_uses_flags(self):
        """Для отчётных документов сравниваются булевы флаги field3_*/field4_*, а группа определяет пороги."""
        a = {"field1_3": [], "field3_0": True, "field3_1": True, "field4_0": False, "field4_1": True, "field4_2": True}
        b = {"field1_3": [], "field3_0": True, "field3_1": False, "field4_0": False, "field4_1": True, "field4_2": True}
        cmp = pc.compare(a, b)
        self.assertEqual((cmp.group, cmp.common, cmp.count()), ("report", 5, 1))
        self.assertEqual(pc.suggest_range(cmp), 1.0)


if __name__ == "__main__":
    unittest.main()

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
        self.assertIn("Не выявлено нарушение по критерию 1.2.", text)   # номер берётся из названия, единственное число
        self.assertEqual(pc.build_comment(pc.compare(SUMMARY, SUMMARY)), pc.POSITIVE_COMMENT)

    def test_comment_follows_grade(self):
        """1.00 — расхождения не перечисляются; 0.75 — «незначительные отклонения»; 0.50 — «существенные расхождения»."""
        cmp = pc.compare({**SUMMARY, "field2_1_2": 1}, SUMMARY)
        self.assertEqual(pc.build_comment(cmp, grade=1.0), pc.POSITIVE_COMMENT)
        self.assertTrue(pc.build_comment(cmp, grade=0.75).startswith("1. Незначительные отклонения"))
        self.assertTrue(pc.build_comment(cmp, grade=0.5).startswith("1. Существенные расхождения"))
        self.assertIn("по критерию field2_1_2", pc.build_comment(cmp, grade=0.5))

    def test_comment_phrases(self):
        """Формулировки по сути ошибки: пропуск, лишнее нарушение, «не предусмотрено» отмечено неверно — единственное и множественное число."""
        primary = {**SUMMARY, "field2_1_1": 0, "field2_1_2": 1, "field2_1_3": 0, "field2_1_4": 1, "field2_1_5": 2}
        summary = {**SUMMARY, "field2_1_5": 1, "field2_1_6": 1}
        text = pc.build_comment(pc.compare(primary, summary))
        self.assertIn("Не выявлено нарушение по критерию field2_1_2.", text)
        self.assertIn("Неверно указаны нарушения по критериям: field2_1_1, field2_1_3.", text)
        self.assertIn("Неверно отмечено «не предусмотрено» по критерию field2_1_5 (требование предусмотрено).", text)
        self.assertIn("Неверно отмечено «в наличии» по критерию field2_1_4 (требование не предусмотрено).", text)

    def test_full_match_rule(self):
        """1.00 снижается до 0.75 при любом расхождении, кроме общих для всех первичных; режимы missfa/missed смягчают правило."""
        other = pc.compare({**SUMMARY, "field2_1_4": 1}, SUMMARY)                       # только «1 против 2»
        missed = pc.compare({**SUMMARY, "field2_1_2": 1}, SUMMARY)
        self.assertEqual(pc.apply_full_match_rule(1.0, other, mode="any"), (0.75, True))
        self.assertEqual(pc.apply_full_match_rule(1.0, other, mode="missfa"), (1.0, False))
        self.assertEqual(pc.apply_full_match_rule(1.0, missed, mode="missed"), (0.75, True))
        self.assertEqual(pc.apply_full_match_rule(1.0, missed, {"field2_1_2"}, mode="any"), (1.0, False))   # общее расхождение
        self.assertEqual(pc.apply_full_match_rule(0.5, missed, mode="any"), (0.5, False))
        self.assertEqual(pc.apply_full_match_rule(1.0, missed, mode=None), (1.0, False))
        self.assertEqual(pc.apply_full_match_rule(1.0, pc.compare(SUMMARY, SUMMARY), mode="any"), (1.0, False))

    def test_suggest_range_thresholds(self):
        """Оценка по числу расхождений; при малом числе общих критериев — None."""
        cmp = pc.Comparison(common=50, differences=[pc.Difference(f"field2_1_{i}", 1, 0) for i in range(8)])
        self.assertEqual(pc.suggest_range(cmp, (7, 10, 22)), 0.0)    # счёт 24 (8 пропусков × 3) ≥ 22
        self.assertEqual(cmp.score((3, 1, 1)), 24)
        self.assertEqual(pc.suggest_range(cmp, (7, 10, 25)), 0.5)
        self.assertEqual(pc.suggest_range(pc.Comparison(common=2), (7, 10, 22)), None)
        self.assertEqual(pc.suggest_range(pc.Comparison(common=50), (7, 10, 22)), 1.0)

    def test_scale_by_analyst(self):
        """Шкала аналитика: 0–25% → 0, 25–50% → 0.5, 50–75% → 0.75, выше 75% → 1; границы включаются в нижний диапазон."""
        got = [pc.range_by_scale(p) for p in (0, 25, 25.1, 50, 75, 75.1, 100, None)]
        self.assertEqual(got, [0.0, 0.0, 0.5, 0.5, 0.75, 1.0, 1.0, None])

    def test_model_prediction(self):
        """Вывод на чистом Python: вероятности в сумме дают 1, класс — с наибольшей вероятностью; без модели группы — None."""
        model = {"procurement": {"features": ["n_missed"], "mean": [0.0], "scale": [1.0], "classes": [0.0, 1.0],
                                 "coef": [[2.0], [-2.0]], "intercept": [0.0, 1.0]}}
        many = pc.predict_range({"n_missed": 3.0}, "procurement", model)
        none = pc.predict_range({"n_missed": 0.0}, "procurement", model)
        self.assertEqual((many["range"], none["range"]), (0.0, 1.0))
        self.assertAlmostEqual(sum(many["probabilities"].values()), 1.0, places=2)
        self.assertIsNone(pc.predict_range({}, "report", model))

    def test_shipped_model_loads(self):
        """Поставляемый JSON модели читается, признаки пары совпадают с признаками модели по типу."""
        feats = pc.features({**SUMMARY, "field2_1_2": 1}, SUMMARY)
        self.assertIn("missed:field2_1_2", feats)
        result = pc.predict_range(feats, "procurement")
        self.assertIn(result["range"], (0.0, 0.5, 0.75, 1.0))

    def test_report_form_uses_flags(self):
        """Для отчётных документов сравниваются булевы флаги field3_*/field4_*, а группа определяет пороги."""
        a = {"field1_3": [], "field3_0": True, "field3_1": True, "field4_0": False, "field4_1": True, "field4_2": True}
        b = {"field1_3": [], "field3_0": True, "field3_1": False, "field4_0": False, "field4_1": True, "field4_2": True}
        cmp = pc.compare(a, b)
        self.assertEqual((cmp.group, cmp.common, cmp.count()), ("report", 5, 1))
        self.assertEqual(pc.suggest_range(cmp), 1.0)


if __name__ == "__main__":
    unittest.main()


class FullMatchExemptionTests(unittest.TestCase):
    """Исключение постпроверки: почти полное совпадение не снижает 1.00."""

    def test_high_match_percent_keeps_full_grade(self):
        """При match_percent выше порога единичное расхождение 1.00 не отменяет; ниже порога — снижает до 0.75."""
        summary = {f"field2_1_{i}": 1 for i in range(1, 41)}
        primary = {**summary, "field2_1_2": 0}           # 1 расхождение из 40: 97.5%
        cmp = pc.compare(primary, summary)
        self.assertGreater(cmp.match_percent, 95)
        self.assertEqual(pc.apply_full_match_rule(1.0, cmp, set(), "any"), (1.0, False))
        few = pc.compare({**{f"field2_1_{i}": 1 for i in range(1, 11)}, "field2_1_2": 0},
                                      {f"field2_1_{i}": 1 for i in range(1, 11)})    # 90%
        self.assertEqual(pc.apply_full_match_rule(1.0, few, set(), "any"), (0.75, True))
        # переданный взвешенный процент важнее обычного match_percent
        self.assertEqual(pc.apply_full_match_rule(1.0, few, set(), "any", percent=96.0), (1.0, False))
        self.assertEqual(pc.apply_full_match_rule(1.0, cmp, set(), "any", percent=80.0), (0.75, True))

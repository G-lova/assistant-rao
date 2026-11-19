"""Тесты модели вероятности упоминания расхождения (knowledge_store.primary_mention) и разметки комментариев."""
import unittest

from knowledge_store import primary_compare, primary_mention as pm
from scripts import fit_primary_mention as fm

SPEC = {"weights": {"missed:field2_1_1": 2.0, "false_alarm:field2_1_2": -2.0}, "bias": 0.0, "n_coef": 0.0,
        "kind_coef": {"missed": 0.0, "false_alarm": 0.0, "other": 0.0}, "min_probability": 0.4}


class MentionTests(unittest.TestCase):
    """Вероятность, отбор для комментария, разметка по номерам критериев."""

    def test_probability_and_select(self):
        """Положительный вес даёт вероятность > 0,5, отрицательный < 0,5; отбор оставляет вероятные, а если таких нет — самые вероятные."""
        a = primary_compare.Difference("field2_1_1", 1, 0)
        b = primary_compare.Difference("field2_1_2", 0, 1)
        probs = pm.probabilities("procurement", [a, b], {"procurement": SPEC})
        self.assertGreater(probs["field2_1_1"], 0.5)
        self.assertLess(probs["field2_1_2"], 0.2)
        self.assertEqual(pm.select([a, b], probs, 0.4), [a])
        self.assertEqual(pm.select([b], probs, 0.4), [b])          # всё ниже порога — остаются самые вероятные
        self.assertEqual(pm.select([a, b], {}, 0.4), [a, b])       # нет модели — без отбора
        self.assertEqual(pm.probabilities("report", [a], {"procurement": SPEC}), {})

    def test_comment_uses_only_probable_differences(self):
        """Комментарий с отбором перечисляет вероятные расхождения, полный перечень — все."""
        cmp = primary_compare.compare({"field2_1_1": 1, "field2_1_2": 0}, {"field2_1_1": 0, "field2_1_2": 1})
        probs = pm.probabilities("procurement", cmp.differences, {"procurement": SPEC})
        labels = {"field2_1_1": "2.1.1. Первый критерий", "field2_1_2": "2.1.2. Второй критерий"}
        short = primary_compare.build_comment(cmp, labels, mention=probs, mention_min=0.4)
        full = primary_compare.build_comment(cmp, labels)
        self.assertIn("2.1.1", short)
        self.assertNotIn("2.1.2", short)
        self.assertIn("2.1.2", full)

    def test_mentioned_numbers(self):
        """Номера берутся из строк «N.N. Название: …»; шаблон «ЭЗ соответствует» — размечаемый случай без номеров; прочее не размечается."""
        text = "2.1.5. Наличие информации о сроке: не указан срок.\n3.2. Соответствие требованиям к описанию: нарушено."
        self.assertEqual(fm.mentioned_numbers(text), {"2.1.5", "3.2"})
        self.assertEqual(fm.mentioned_numbers("ЭЗ соответствует предъявляемым требованиям"), set())
        self.assertIsNone(fm.mentioned_numbers("Замечания по оформлению"))


if __name__ == "__main__":
    unittest.main()

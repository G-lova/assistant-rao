"""Тесты взвешенного процента сходства и шкалы порогов (на небольшой собственной модели, без файла весов)."""
import unittest

from knowledge_store import primary_compare, primary_score

# 10 критериев; пропуск критерия 1 тяжёлый, лишнее нарушение критерия 2 слабое, расхождение критерия 3 «прощается» (вес < 0)
SPEC = {"weights": {"missed:field2_1_1": 0.4, "false_alarm:field2_1_2": 0.1, "missed:field2_1_3": -0.1},
        "w_avg": 0.2, "w_default": 0.2, "thresholds": [30.0, 60.0, 90.0]}
MODEL = {"procurement": SPEC}
ALL_OK = {f"field2_1_{i}": 1 for i in range(1, 11)}


def compare_with(primary=None, summary=None):
    """Сравнение заключений из 10 общих критериев; все «1», кроме заданных. Пропуск: первичный 1, итоговый 0;
    лишнее нарушение: первичный 0, итоговый 1."""
    return primary_compare.compare({**ALL_OK, **(primary or {})}, {**ALL_OK, **(summary or {})})


class WeightedPercentTests(unittest.TestCase):
    """Процент, вычеты, оценка по порогам и пограничная оценка."""

    def test_full_match_is_100_without_deductions(self):
        """Без расхождений — 100% и оценка 1.0."""
        result = primary_score.evaluate(compare_with(), MODEL)
        self.assertEqual((result["percent"], result["deductions"], result["range"]), (100.0, [], 1.0))

    def test_weights_and_sorting(self):
        """Вычет = 100·вес/(критериев·w_avg): пропуск 1 снимает 20 п. п., лишнее 2 — 5; сортировка по убыванию."""
        miss = primary_score.evaluate(compare_with(summary={"field2_1_1": 0}), MODEL)
        self.assertEqual(miss["percent"], 80.0)
        self.assertEqual(miss["deductions"][0], {"key": "field2_1_1", "kind": "missed", "percent": 20.0})
        both = primary_score.evaluate(compare_with({"field2_1_2": 0}, {"field2_1_1": 0}), MODEL)
        self.assertEqual(both["percent"], 75.0)
        self.assertEqual([d["key"] for d in both["deductions"]], ["field2_1_1", "field2_1_2"])

    def test_forgiven_difference_adds_percent_but_not_over_100(self):
        """Отрицательный вес даёт отрицательный вычет; процент не превышает 100."""
        result = primary_score.evaluate(compare_with(summary={"field2_1_3": 0}), MODEL)
        self.assertEqual(result["deductions"][0]["percent"], -5.0)
        self.assertEqual(result["percent"], 100.0)

    def test_grade_thresholds_and_unknown_weight(self):
        """Оценка по порогам: <30 → 0, <60 → 0.5, <90 → 0.75; неизвестное расхождение берёт w_default."""
        for percent, grade in ((10, 0.0), (30, 0.5), (59.9, 0.5), (60, 0.75), (89.9, 0.75), (90, 1.0), (None, None)):
            self.assertEqual(primary_score.grade_by_percent(percent, SPEC), grade)
        unknown = primary_score.evaluate(compare_with({"field2_1_9": 0}), MODEL)
        self.assertEqual(unknown["percent"], 90.0)          # w_default = w_avg → 10 п. п.

    def test_neighbour_grade_near_threshold(self):
        """Пограничный процент: вторая оценка — в сторону порога; вдали от порогов — None."""
        self.assertEqual(primary_score.neighbour_grade(89.0, SPEC, 2.0), 1.0)
        self.assertEqual(primary_score.neighbour_grade(60.5, SPEC, 2.0), 0.5)
        self.assertIsNone(primary_score.neighbour_grade(75.0, SPEC, 2.0))

    def test_missing_model_file(self):
        """Нет файла весов — пустая модель, оценка не определяется (пайплайн уходит на запасные способы)."""
        self.assertEqual(primary_score.load_model("/nonexistent/model.json"), {})
        self.assertIsNone(primary_score.evaluate(compare_with(), {}))


if __name__ == "__main__":
    unittest.main()

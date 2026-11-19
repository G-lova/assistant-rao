"""Тесты таксономии замечаний."""
import unittest

from knowledge_store import primary_taxonomy as tx


class TaxonomyTests(unittest.TestCase):
    """Категория расхождения по виду и разделу формы; подсчёт и порядок категорий."""

    def test_eight_categories(self):
        """В таксономии восемь категорий аналитика, названия без искажений."""
        self.assertEqual(list(tx.CATEGORIES.values()), [
            "Неполнота", "Фактические ошибки", "Методологические ошибки", "Нормативные ошибки",
            "Недостаточная аргументация", "Противоречия", "Формальные недостатки", "Существенные пропуски"])
        self.assertEqual(sorted(tx.COMMENT_ORDER), sorted(tx.CATEGORIES))

    def test_difference_category(self):
        """Пропуск — «Существенные пропуски»; в разделе 1 закупок неверный ответ — фактическая, в разделах 2–4 — нормативная; в отчётных — фактическая."""
        self.assertEqual(tx.difference_category("missed", "field2_3_7"), "MISSED_VIOLATION")
        self.assertEqual(tx.difference_category("false_alarm", "field2_1_29"), "FACTUAL")
        self.assertEqual(tx.difference_category("other", "field2_1_12"), "FACTUAL")
        self.assertEqual(tx.difference_category("false_alarm", "field2_3_7"), "NORMATIVE")
        self.assertEqual(tx.difference_category("false_alarm", "field2_2_3"), "NORMATIVE")
        self.assertEqual(tx.difference_category("false_alarm", "field4_1", "report"), "FACTUAL")
        self.assertEqual(tx.difference_category("missed", "field4_1", "report"), "MISSED_VIOLATION")

    def test_categorize_counts_in_taxonomy_order(self):
        """Расхождения и коды складываются по категориям; пустые категории не выводятся; порядок как в CATEGORIES."""
        result = tx.categorize([("false_alarm", "field2_1_2"), ("missed", "field2_1_3"), ("false_alarm", "field2_3_7")],
                               ["EMPTY_DOCS", "UNKNOWN"])
        self.assertEqual([(c["code"], c["count"]) for c in result],
                         [("INCOMPLETE", 1), ("FACTUAL", 1), ("NORMATIVE", 1), ("MISSED_VIOLATION", 1)])
        self.assertEqual(tx.categorize(), [])
        # в строке комментария пропуск назван нейтрально (тяжесть задаёт оценка), остальные названия те же
        self.assertEqual(tx.COMMENT_NAMES["MISSED_VIOLATION"], "Пропущенные нарушения")
        self.assertEqual(tx.COMMENT_NAMES["NORMATIVE"], tx.CATEGORIES["NORMATIVE"])


if __name__ == "__main__":
    unittest.main()

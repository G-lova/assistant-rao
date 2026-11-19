"""Тесты таксономии замечаний."""
import unittest

from knowledge_store import primary_taxonomy as tx


class TaxonomyTests(unittest.TestCase):
    """Категория расхождения по виду и разделу формы; подсчёт и порядок категорий."""

    def test_eight_categories(self):
        """В таксономии восемь категорий, названия — как согласовано; порядок строк комментария покрывает все категории."""
        self.assertEqual(list(tx.CATEGORIES.values()), [
            "Неполнота", "Фактические ошибки", "Нормативные ошибки", "Недостаточная аргументация", "Противоречия",
            "Формальные недостатки", "Пропуски нарушений", "Несоответствия"])
        self.assertEqual(sorted(tx.COMMENT_ORDER), sorted(tx.CATEGORIES))
        self.assertEqual(tx.COMMENT_NAMES, tx.CATEGORIES)

    def test_difference_category(self):
        """Пропуск — «Пропуски нарушений»; раздел 1 закупок: не учтённая информация — тоже пропуск; остальное — «Несоответствия»."""
        self.assertEqual(tx.difference_category("missed", "field2_3_7"), "MISSED_VIOLATION")
        self.assertEqual(tx.difference_category("false_alarm", "field2_1_29"), "MISSED_VIOLATION")
        self.assertEqual(tx.difference_category("other", "field2_1_12", primary=2), "MISSED_VIOLATION")   # «не предусмотрено», а требование есть
        self.assertEqual(tx.difference_category("other", "field2_1_12", primary=1), "MISMATCH")           # «в наличии», а требования нет
        self.assertEqual(tx.difference_category("false_alarm", "field2_3_7"), "MISMATCH")
        self.assertEqual(tx.difference_category("false_alarm", "field4_1", "report"), "MISMATCH")
        self.assertEqual(tx.difference_category("missed", "field4_1", "report"), "MISSED_VIOLATION")

    def test_categorize_counts_in_taxonomy_order(self):
        """Расхождения и коды складываются по категориям; пустые категории не выводятся; порядок как в CATEGORIES."""
        result = tx.categorize([("other", "field2_1_2", 1), ("missed", "field2_1_3", 1), ("false_alarm", "field2_3_7", 0)],
                               ["EMPTY_DOCS", "UNKNOWN"])
        self.assertEqual([(c["code"], c["count"]) for c in result],
                         [("INCOMPLETE", 1), ("MISSED_VIOLATION", 1), ("MISMATCH", 2)])
        self.assertEqual(tx.categorize(), [])


if __name__ == "__main__":
    unittest.main()

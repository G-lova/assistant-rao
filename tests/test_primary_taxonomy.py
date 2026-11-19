"""Тесты таксономии замечаний."""
import unittest

from knowledge_store import primary_taxonomy


class TaxonomyTests(unittest.TestCase):
    """Подсчёт категорий по видам расхождений и кодам дефектов."""

    def test_categorize_counts_in_taxonomy_order(self):
        """Расхождения и коды складываются по категориям; пустые категории не выводятся; порядок как в CATEGORIES."""
        result = primary_taxonomy.categorize(["false_alarm", "missed", "false_alarm"], ["EMPTY_DOCS", "UNKNOWN"])
        self.assertEqual([(c["code"], c["count"]) for c in result],
                         [("MISSED_VIOLATION", 1), ("INCOMPLETE", 1), ("FACTUAL", 2)])
        self.assertEqual(result[0]["name"], "Существенные пропуски")
        self.assertEqual(primary_taxonomy.categorize(), [])


if __name__ == "__main__":
    unittest.main()

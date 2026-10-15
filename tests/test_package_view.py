"""Тесты представления комплекта документов (filename/url, вход проверки полноты)."""
import unittest

from evaluate_documents.package_view import completeness_input


class PackageViewTests(unittest.TestCase):
    """Проверки чистых функций ``evaluate_documents.package_view``."""

    def test_completeness_input_drops_package_level_keys(self):
        """Отсутствующие документы и факты не попадают во вход полноты типа."""
        base = {"facts": {"x": 1}, "missed_documents": ["Обоснование НМЦК"], "documents": [1], "law": "44-ФЗ"}
        res = completeness_input(base, "docIzvejenieFiles", "Извещение", [{"a": 1}])
        self.assertNotIn("missed_documents", res)
        self.assertNotIn("facts", res)
        self.assertEqual(res["documents"], [{"a": 1}])
        self.assertEqual(res["law"], "44-ФЗ")
        self.assertIn("missed_documents", base)  # исходный контекст не изменён


if __name__ == "__main__":
    unittest.main()

"""Тесты представления комплекта документов (filename/url, вход проверки полноты)."""
import unittest

from evaluate_documents.package_view import completeness_input, files_view, with_source


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

    def test_with_source_dict_and_list_keeps_existing(self):
        """filename/url проставляются в dict и list; заполненные значения сохраняются."""
        one = with_source({}, "a.pdf", "http://u/a")
        self.assertEqual((one["filename"], one["url"]), ("a.pdf", "http://u/a"))
        many = with_source([{"filename": "x"}, {}], "b.pdf", "http://u/b")
        self.assertEqual(many[0]["filename"], "x")
        self.assertEqual(many[1]["url"], "http://u/b")

    def test_files_view(self):
        """Список файлов содержит имя, ссылку, тип и статус читаемости."""
        docs = [{"filename": "a.pdf", "url": "u", "type_compliance": {"detected_type": "t"},
                 "readability": {"status": "deny"}}, "мусор"]
        self.assertEqual(files_view(docs), [{"filename": "a.pdf", "url": "u", "detected_type": "t", "readability": "deny"}])


if __name__ == "__main__":
    unittest.main()

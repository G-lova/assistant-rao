"""Тесты проверок первичного заключения (knowledge_store.primary_review)."""
import unittest

from knowledge_store import primary_review as pr


class PrimaryReviewTests(unittest.TestCase):
    """Объективные дефекты заключений закупки и отчётных документов."""

    def test_clean_opinion_has_no_issues(self):
        """Заполненное заключение без нулей и заглушек замечаний не даёт."""
        data = {"documents": "Извещение, проект контракта, обоснование НМЦК", "field2_1_1": 1, "field2_1_2": 1,
                "field3": "", "field4": "На основании представленной документации целесообразно осуществить закупку."}
        self.assertEqual(pr.review_opinion(data), [])

    def test_zero_without_remarks_and_junk(self):
        """«0» при пустых замечаниях, пустой перечень документов и «тест» находятся."""
        data = {"documents": "", "field2_1_1": 0, "field2_1_2_text": "тест", "field3": "", "field4": "____"}
        codes = {i.code for i in pr.review_opinion(data)}
        self.assertEqual(codes, {"EMPTY_DOCS", "JUNK_TEXT", "ZERO_NO_REMARKS", "CONCLUSION_TEMPLATE"})

    def test_report_form(self):
        """Заключение по отчётным документам: нет позиций / позиция без названия."""
        self.assertIn("NO_ITEMS", {i.code for i in pr.review_opinion({"field1_1": "Акт от 01.02.2026 № 5", "field1_3": []})})
        data = {"field1_1": "Акт от 01.02.2026 № 5", "field1_3": [{"name": ""}]}
        self.assertIn("ITEM_NO_NAME", {i.code for i in pr.review_opinion(data)})

    def test_normalize(self):
        """Булевы и строковые значения приводятся к 1/0/2."""
        self.assertEqual([pr.normalize(v) for v in (True, "false", 2, "x", None)], [1, 0, 2, None, None])


class TestExpertsTests(unittest.TestCase):
    """Тестовые пользователи исключаются из обучения."""

    def test_is_test_expert(self):
        """Идентификаторы из настроек (в том числе строкой, как в CSV) — тестовые; остальные и мусор — нет."""
        from scripts.eval_primary_opinion import is_test_expert
        self.assertTrue(is_test_expert("3") and is_test_expert(1638))
        self.assertFalse(is_test_expert("1645") or is_test_expert("NULL") or is_test_expert(None))


if __name__ == "__main__":
    unittest.main()

"""Тесты проверки ссылок на нормы в замечаниях первичных (scripts.check_primary_legal_refs)."""
import unittest

from scripts import check_primary_legal_refs as cl


class LegalRefsScriptTests(unittest.TestCase):
    """Извлечение ссылок из замечаний и сводка статусов."""

    def test_remark_texts_take_field3_and_text_fields(self):
        """Берутся замечания ``field3`` и поля ``*_text``; пустые и нестроковые пропускаются."""
        data = {"field3": "1.2. Нарушена ч. 3 ст. 7 44-ФЗ", "field2_1_2_text": "п. 5 ст. 3", "field2_1_3_text": "", "field4": "ст. 1"}
        self.assertEqual(cl.remark_texts(data), ["1.2. Нарушена ч. 3 ст. 7 44-ФЗ", "п. 5 ст. 3"])

    def test_extract_refs_uses_default_act(self):
        """Ссылка без названия закона получает акт по умолчанию."""
        refs = cl.extract_refs({"field3": "Нарушена ч. 3 ст. 7"}, "223-ФЗ")
        self.assertEqual([(r["act"], r["article"], r["part"]) for r in refs], [("223-ФЗ", "7", "3")])

    def test_summarize_counts_and_mean_range(self):
        """Заключение с not_found считается плохим; средняя оценка считается отдельно для плохих и прочих."""
        rows = [{"expertise_id": "1", "expert_id": "5", "status": "not_found"},
                {"expertise_id": "1", "expert_id": "5", "status": "found"},
                {"expertise_id": "2", "expert_id": "5", "status": "found"}]
        experts = {("1", "5"): {"range": "0.5"}, ("2", "5"): {"range": "1.0"}}
        result = cl.summarize(rows, experts)
        self.assertEqual(result["refs"], {"not_found": 1, "found": 2})
        self.assertEqual((result["opinions"], result["bad_opinions"]), (2, 1))
        self.assertEqual((result["mean_range_bad"], result["mean_range_ok"]), (0.5, 1.0))


if __name__ == "__main__":
    unittest.main()

"""Тесты черновика комментариев РАО (``rao_comment``) и режима «по запросу»."""
import unittest

from knowledge_store import rao_comments, summary


class RaoCommentsTests(unittest.TestCase):
    """Сборка ``rao_comment`` по результатам оценки первичных заключений."""

    def test_keys_and_texts(self):
        """Ключ — ``<поле>-<expert_id>``; общие расхождения пропускаются; замечания с полем склеиваются."""
        reviews = [
            {"expert_id": 3, "differences": [
                {"key": "field2_1_1", "kind": "missed", "shared": False},
                {"key": "field2_1_2", "kind": "false_alarm", "shared": False},
                {"key": "field2_1_3", "kind": "missed", "shared": True}],
             "issues": [{"code": "FACT_MISMATCH", "message": "неверно указан ответ", "keys": ["field5_0"]},
                        {"code": "FORMAL", "message": "орфография", "keys": []}]},
            {"expert_id": 4, "differences": [{"key": "field2_1_1", "kind": "other", "shared": False}], "issues": []},
        ]
        got = rao_comments.from_reviews(reviews)
        self.assertEqual(set(got), {"field2_1_1-3", "field2_1_2-3", "field5_0-3", "field2_1_1-4"})
        self.assertIn("не выявлено", got["field2_1_1-3"])
        self.assertIn("не подтверждается", got["field2_1_2-3"])
        self.assertEqual(got["field5_0-3"], "Неверно указан ответ.")
        self.assertEqual(rao_comments.from_reviews([{"expert_id": 1, "differences": [], "issues": []}]), {})

    def test_validate_accepts_rao_comment_dict(self):
        """У сводного эксперта ``rao_comment`` — словарь, а не текст: ``validate`` его допускает."""
        fields = [{"field_key": "rao_comment", "value_kind": "text"}]
        self.assertEqual(summary.validate({"rao_comment": {"field1-3": "ок"}}, fields), [])
        self.assertEqual(summary.validate({"rao_comment": None}, fields), [])
        self.assertTrue(summary.validate({"rao_comment": 5}, fields))


class AddRaoCommentsTests(unittest.IsolatedAsyncioTestCase):
    """Подмешивание комментариев в ``data`` только по запросу."""

    async def test_only_on_request(self):
        """Без фабрики ``data`` не меняется; с фабрикой — словарь или ``None``, сбой не роняет сборку."""
        async def ok(data):
            return {"field1-3": "текст"}

        async def empty(data):
            return {}

        async def broken(data):
            raise RuntimeError("БД недоступна")

        for factory, expected in ((None, None), (ok, {"field1-3": "текст"}), (empty, None), (broken, None)):
            data = {"field1": 1, "rao_comment": None}
            await summary.add_rao_comments(data, factory)
            self.assertEqual(data["rao_comment"], expected)
        data = {"field1": 1}                      # у формы нет служебного поля — ключ не добавляется
        await summary.add_rao_comments(data, ok)
        self.assertEqual(data, {"field1": 1})


if __name__ == "__main__":
    unittest.main()

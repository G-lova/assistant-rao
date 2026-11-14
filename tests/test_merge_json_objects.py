"""Ответ модели после ``repair_json`` — список не-словарей: извлечение не падает на ``dict.update``.

Prod, 14.11.2026: «Приложение № 3 Требования к содержанию заявки…doc» — ответ обрезан (finish_reason=length),
``repair_json`` дал список, ``merge_json_objects`` упал «dictionary update sequence element #0 has length 4».
"""
import unittest


class MergeJsonObjectsTest(unittest.TestCase):
    """``merge_utils.merge_json_objects`` / ``as_json_object``."""

    def test_merge_skips_non_dicts(self):
        from evaluate_documents.merge_utils import merge_json_objects

        self.assertEqual(merge_json_objects(["abcd", 5, None, {"a": 1}, '{"b": 2}', [{"c": 3}, "x"]]),
                         {"a": 1, "b": 2, "c": 3})
        self.assertEqual(merge_json_objects(["text", 1]), {})

    def test_as_json_object(self):
        from evaluate_documents.merge_utils import as_json_object

        self.assertEqual(as_json_object({"a": 1}), {"a": 1})
        self.assertEqual(as_json_object([{"a": 1}, "мусор"]), {"a": 1})
        for bad in (["abcd"], [], "строка", 3, None):
            self.assertIsNone(as_json_object(bad))


if __name__ == "__main__":
    unittest.main()

"""Тип документа: не-словарь из ответа модели или из кэша не роняет анализ файла.

Stage, 14.11.2026: файлы 31611, 31650, 31651 … падали в ``_doc_type`` на ``dict(info)`` («dictionary update sequence
element #0 has length 4», «'int' object is not iterable») — в кэше ``doc_type`` лежал список / число из ответа модели.
"""
import asyncio
import sys
import types
import unittest


def setUpModule():
    """``json_repair`` может не стоять в окружении тестов — подменяется заглушкой (в образе пакет есть)."""
    if "json_repair" not in sys.modules:
        try:
            import json_repair  # noqa: F401
        except ImportError:
            sys.modules["json_repair"] = types.SimpleNamespace(repair_json=lambda s: s)


class AsTypeResultTest(unittest.TestCase):
    """``type_detector.as_type_result``."""

    def test_shapes(self):
        from evaluate_documents.type_detector import as_type_result

        d = {"detected_type": "docOpusObjectZacupFiles", "confidence": 0.9, "issues": [], "key_indicators": []}
        self.assertEqual(as_type_result(d), d)
        self.assertEqual(as_type_result([d, {"x": 1}]), d)
        for bad in ([1, 2], ["текст"], "строка", 5, [], None):
            self.assertIsNone(as_type_result(bad))


class DocTypeCacheTest(unittest.TestCase):
    """``AIFileAnalyzer._doc_type``: негодный кэш — тип определяется заново и не сохраняется, если снова не словарь."""

    def analyzer(self, cached, produced):
        from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

        a = AIFileAnalyzer.__new__(AIFileAnalyzer)
        a.recompute = False
        a.extraction_model = "m"
        a.versions = {"doc_type": "v"}
        saved = []

        class Repo:
            """Кэш ``ai_document_analysis``."""

            async def get_analysis(self, doc, kind, ver, model):
                return {"result": cached} if cached is not None else None

            async def save_analysis(self, *args):
                saved.append(args[4])

        class Detector:
            """``TypeDetector``."""

            async def detect_document_type(self, content, filename):
                return produced

            def detect_document_type_by_name(self, name):
                return None

        a.repo = Repo()
        a.type_detector = Detector()
        return a, saved

    def run_doc_type(self, a):
        """``_doc_type`` с заглушкой ``type_data_extractor`` (его импорт тянет пакеты, которых может не быть)."""
        from unittest import mock

        stub = types.SimpleNamespace(ALLOWED_DOC_TYPES=["docActFiles", "docDopMaterialsFiles"],
                                     DOCUMENT_TYPE_MAPPING={"docActFiles": "Акт",
                                                            "docDopMaterialsFiles": "Дополнительные материалы"})
        with mock.patch.dict(sys.modules, {"evaluate_documents.type_data_extractor": stub}):
            return asyncio.run(a._doc_type(1, ["текст"], "акт.pdf", None, {"owner_type": "contract"}))

    def test_bad_cache_recomputed(self):
        good = {"detected_type": "docActFiles", "confidence": 0.95, "issues": [], "key_indicators": []}
        a, saved = self.analyzer([{"a": 1, "b": 2, "c": 3, "d": 4}, 7], good)
        info, _ = self.run_doc_type(a)
        self.assertEqual(saved, [good])
        self.assertIn("detected_type", info)

    def test_bad_answer_not_cached(self):
        a, saved = self.analyzer(None, [1, 2, 3])
        info, _ = self.run_doc_type(a)
        self.assertEqual(saved, [])
        self.assertTrue(info["detected_type"])   # тип по имени файла / запасной


if __name__ == "__main__":
    unittest.main()

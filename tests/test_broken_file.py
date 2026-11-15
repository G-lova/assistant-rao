"""Повреждённый файл (PDF без trailer / xref, «EOF marker not found»).

Прод, 15.11.2026: «Экспертное заключение. doc.pdf» (файл 370552) — «не удалось определить число страниц», ошибка
анализа с трассировкой и повтором при каждом прогоне. Теперь — ссылка ЕИС (копия в хранилище бывает обрезанной),
иначе документ «нет текста».
"""
import asyncio
import unittest

from configs.file_errors import BrokenFile


def analyzer(calls, broken_sources):
    """``AIFileAnalyzer`` с подменённым ``_load_document``: источники из ``broken_sources`` — повреждённый файл."""
    from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

    a = AIFileAnalyzer.__new__(AIFileAnalyzer)

    async def load(file_id, url, file_name, card_id=None, local_path=None, broken_ok=True):
        calls.append((url, broken_ok))
        if url in broken_sources:
            if broken_ok:
                return 1, None, "sha", 10      # документ «нет текста»
            raise BrokenFile("не удалось определить число страниц")
        return 2, "текст", "sha", 10

    a._load_document = load
    return a


class FallbackTest(unittest.TestCase):
    """``_load_with_fallback``."""

    def test_broken_storage_copy_eis_ok(self):
        calls = []
        a = analyzer(calls, {"https://storage/x.pdf"})
        row = {"url": "https://storage/x.pdf", "source_url": "https://zakupki.gov.ru/x"}
        res = asyncio.run(a._load_with_fallback(370552, row, "x.pdf"))
        self.assertEqual(res[1], "текст")
        self.assertEqual(calls, [("https://storage/x.pdf", False), ("https://zakupki.gov.ru/x", True)])

    def test_broken_everywhere_no_text(self):
        calls = []
        a = analyzer(calls, {"https://storage/x.pdf", "https://zakupki.gov.ru/x"})
        row = {"url": "https://storage/x.pdf", "source_url": "https://zakupki.gov.ru/x"}
        res = asyncio.run(a._load_with_fallback(370552, row, "x.pdf"))
        self.assertIsNone(res[1])

    def test_no_alternative(self):
        calls = []
        a = analyzer(calls, {"https://storage/x.pdf"})
        res = asyncio.run(a._load_with_fallback(1, {"url": "https://storage/x.pdf", "source_url": None}, "x.pdf"))
        self.assertIsNone(res[1])
        self.assertEqual(calls, [("https://storage/x.pdf", True)])


class ReadPdfTest(unittest.TestCase):
    """``FileReader.read_file``: PDF без структуры — ``BrokenFile`` (а не общий ``ValueError``)."""

    def test_truncated_pdf(self):
        import os
        import tempfile

        from tests.stubs import stubbed_import

        with stubbed_import("configs.file_reader") as module:
            reader = module.FileReader.__new__(module.FileReader)
            path = os.path.join(tempfile.mkdtemp(), "x.pdf")
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n")   # обрезан: нет xref и trailer
            module.pdf_layer_pages = lambda p: []
            module.pdf_page_count = lambda p: 0
            with self.assertRaises(module.BrokenFile):
                asyncio.run(reader.read_pdf_file(path, "x.pdf"))


if __name__ == "__main__":
    unittest.main()

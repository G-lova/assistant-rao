"""Расширение скачанного файла (``ai_file_analyzer.file_ext``): имена печатных форм ЕИС без расширения."""
import os
import tempfile
import unittest

from tests.stubs import stubbed_import


class FileExtTest(unittest.TestCase):
    """Расширение из имени, из скачивания или по содержимому."""

    def setUp(self):
        """Функция модуля без тяжёлых зависимостей (заглушки действуют только на время импорта)."""
        with stubbed_import("risk_monitoring.ai_file_analyzer") as module:
            self.m = type("M", (), {"file_ext": staticmethod(module.file_ext)})

    def tmp(self, data: bytes) -> str:
        """Временный файл с содержимым."""
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_print_form_name(self):
        """«… (версия 1, док. 1781…0006)» — не расширение; по содержимому — HTML (сбой 30.10.2026)."""
        name = "Печатная форма контракта № 1781204352226000023 (версия 1, док. 17812043522260000230006)"
        self.assertEqual(self.m.file_ext(name, None, self.tmp(b"<!DOCTYPE html><html><body>")), ".html")

    def test_name_and_download(self):
        """Обычное имя — его расширение; без него — расширение скачивания; PDF — по сигнатуре."""
        self.assertEqual(self.m.file_ext("Проект контракта.DOCX", ".pdf"), ".docx")
        self.assertEqual(self.m.file_ext("без расширения", ".xlsx"), ".xlsx")
        self.assertEqual(self.m.file_ext("скан", ".bin", self.tmp(b"%PDF-1.7")), ".pdf")


if __name__ == "__main__":
    unittest.main()


class DownloadFallbackTest(unittest.TestCase):
    """Файл из хранилища внешней системы не скачался (403, 06.11.2026) — повтор по ссылке ЕИС."""

    def _analyzer(self, ok_urls):
        """Анализатор без __init__: _load_document скачивает только ссылки из ``ok_urls``."""
        import asyncio  # noqa: F401

        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.ai_file_analyzer") as m:
            calls = []

            async def load(self, file_id, url, file_name, card_id=None, **kw):
                """Скачивание: успех только для разрешённых ссылок."""
                calls.append(url)
                if url not in ok_urls:
                    raise m.FileUnavailable("HTTP 403: не удалось скачать файл")
                return 1, "текст", "sha", 5

            a = object.__new__(m.AIFileAnalyzer)
            a._load_document = load.__get__(a)
            return m, a, calls

    def test_fallback(self):
        """403 из хранилища → ЕИС; оба 403 — в ошибке оба источника с хостами; без source_url — без повтора."""
        import asyncio

        row = {"url": "https://storage.example/files/a.docx?token=x", "source_url": "https://zakupki.gov.ru/f?uid=1",
               "risk_monitoring_contract_id": 5}
        m, a, calls = self._analyzer({row["source_url"]})
        self.assertEqual(asyncio.run(a._load_with_fallback(7, row, "a.docx"))[1], "текст")
        self.assertEqual(calls, [row["url"], row["source_url"]])

        m, a, calls = self._analyzer(set())
        with self.assertRaises(m.FileUnavailable) as e:
            asyncio.run(a._load_with_fallback(7, row, "a.docx"))
        self.assertIn("хранилище storage.example", str(e.exception))
        self.assertIn("ЕИС zakupki.gov.ru", str(e.exception))
        self.assertNotIn("token", str(e.exception))

        m, a, calls = self._analyzer(set())
        with self.assertRaises(m.FileUnavailable):
            asyncio.run(a._load_with_fallback(7, {**row, "source_url": None}, "a.docx"))
        self.assertEqual(len(calls), 1)

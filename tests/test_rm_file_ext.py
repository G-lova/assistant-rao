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

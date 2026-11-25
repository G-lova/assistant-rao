"""Чтение таблиц (25.11.2026): формат по содержимому, .xls напрямую через xlrd, LibreOffice → CSV, нечитаемая
таблица — BrokenFile (без повторов анализа)."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def _module():
    """``configs.file_reader`` с заглушками тяжёлых зависимостей."""
    from tests.stubs import stubbed_import

    return stubbed_import("configs.file_reader")


class SniffTest(unittest.TestCase):
    """Формат по первым байтам."""

    def test_kinds(self):
        """OLE2, ZIP, HTML / XML, прочее."""
        with _module() as fr, tempfile.TemporaryDirectory() as d:
            cases = {b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest": "ole2", b"PK\x03\x04rest": "zip",
                     b"\xef\xbb\xbf  <html><table>": "markup", b"<?xml version='1.0'?><Workbook": "markup",
                     b"garbage": "unknown"}
            for i, (data, kind) in enumerate(cases.items()):
                p = os.path.join(d, f"f{i}.xls")
                with open(p, "wb") as f:
                    f.write(data)
                self.assertEqual(fr.sniff_spreadsheet(p), kind)


class XlsDirectTest(unittest.TestCase):
    """.xls — через xlrd.open_workbook (не через pandas): числа без «.0», пустые ячейки пропускаются, предел строк."""

    def test_xlrd(self):
        """Заглушка xlrd с одним листом."""
        class Sheet:
            """Лист."""
            name, nrows, ncols = "Обоснование", 3, 3

            def row_values(self, r, start, end):
                """Строка."""
                return [["№", "Наименование", "Цена"], [1.0, "Шприц", 12.5], ["", None, ""]][r][start:end]

        class Book:
            """Книга."""
            nsheets = 1

            def sheet_by_index(self, n):
                """Лист по номеру."""
                return Sheet()

            def unload_sheet(self, n):
                """Выгрузка листа."""

            def release_resources(self):
                """Освобождение."""

        fake = types.SimpleNamespace(open_workbook=lambda path, on_demand=True: Book())
        with _module() as fr, mock.patch.dict(sys.modules, {"xlrd": fake}):
            text = fr.xls_text_limited("x.xls")
        self.assertEqual(text, "Лист: Обоснование\n№ | Наименование | Цена\n1 | Шприц | 12.5")


@unittest.skipUnless(shutil.which("soffice"), "нет LibreOffice")
class LibreOfficeCsvTest(unittest.TestCase):
    """Таблица, которую не читают ни openpyxl, ни LibreOffice → xlsx, читается через CSV; нечитаемая — BrokenFile."""

    def test_ods_named_xls(self):
        """ODS с расширением .xls (ZIP без части workbook — как «Обоснование НМЦК 87-ЭА 2026.xls»)."""
        with _module() as fr, tempfile.TemporaryDirectory() as d:
            # исходная таблица — HTML с явной кодировкой: CSV LibreOffice читает в кодировке системы, и в контейнере
            # CI без локали UTF-8 кириллица портилась уже в исходной таблице теста (25.11.2026)
            src = os.path.join(d, "t.html")
            with open(src, "w", encoding="utf-8") as f:
                f.write('<html><head><meta charset="utf-8"></head><body><table>'
                        '<tr><td>Наименование</td><td>Цена</td></tr><tr><td>Шприц</td><td>12.5</td></tr>'
                        '</table></body></html>')
            subprocess.run(["soffice", "--headless", "--infilter=HTML (StarCalc)", "--convert-to", "ods",
                            "--outdir", d, src], check=True, capture_output=True, timeout=120)
            path = os.path.join(d, "Обоснование.xls")
            os.rename(os.path.join(d, "t.ods"), path)
            self.assertEqual(fr.sniff_spreadsheet(path), "zip")
            reader = fr.FileReader.__new__(fr.FileReader)

            async def broken_xlsx(*a, **kw):
                """xlsx после конвертации не читается (как в логе 25.11)."""
                raise RuntimeError("StartTag: invalid element name")

            with mock.patch.object(fr, "xlsx_text_limited", side_effect=OSError("File contains no valid workbook part")), \
                    mock.patch.object(fr.FileReader, "read_excel_with_libreoffice", broken_xlsx):
                text = asyncio.run(reader.read_excel_file(path, ".xls"))
            self.assertIn("Шприц | 12.5", text)

    def test_broken(self):
        """Таблица без текста (пустой файл с расширением .xls) — BrokenFile, а не ошибка с повторами."""
        with _module() as fr, tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "мусор.xls")
            with open(path, "wb") as f:
                f.write(b"")
            reader = fr.FileReader.__new__(fr.FileReader)
            with self.assertRaises(fr.BrokenFile):
                asyncio.run(reader.read_excel_file(path, ".xls"))


if __name__ == "__main__":
    unittest.main()

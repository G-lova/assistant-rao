"""Тесты выгрузки данных хранилища знаний (без БД: подставное соединение)."""
import asyncio
import csv
import os
import tempfile
import unittest

from scripts import export_primary_facts as ex


class FakeConn:
    """Подставное соединение: по тексту запроса отдаёт строки или запоминает параметры."""

    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql.split("FROM")[1].split()[0], args))
        if "DISTINCT expertise_id" in sql:
            return [{"expertise_id": 7}, {"expertise_id": 9}]
        if "pe_documents" in sql:
            return [{"expertise_id": 7, "id": 1, "filename": "Контракт.pdf", "has_text": True,
                     "extraction": {"number": "12", "date": "01.02.2024"}}]
        return []


class ExportTests(unittest.TestCase):
    """CSV пишутся для всех таблиц; JSON — строкой; полный текст не выгружается."""

    def test_export_writes_all_files(self):
        """Четыре файла, заголовки, JSON в ячейке, пустые таблицы — только колонка expertise_id."""
        conn = FakeConn()
        with tempfile.TemporaryDirectory() as out:
            counts = asyncio.run(ex.export(conn, "dev", out))
            self.assertEqual(counts, {"pe_documents": 1, "pe_procurements": 0, "pe_facts": 0, "pe_summary_opinions": 0, "expertises": 2})
            with open(os.path.join(out, "pe_documents.csv"), encoding="utf-8") as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual(row["filename"], "Контракт.pdf")
            self.assertIn('"number": "12"', row["extraction"])
            self.assertNotIn("text_full", row)
            self.assertTrue(os.path.exists(os.path.join(out, "pe_facts.csv")))
        self.assertEqual(conn.calls[1][1], ("dev", [7, 9]))        # без --ids берутся все экспертизы с документами

    def test_sql_never_selects_full_text(self):
        """В запросах нет выгрузки текста документа: только признак его наличия."""
        self.assertNotIn("text_full,", ex.SQL_DOCUMENTS.replace("(text_full IS NOT NULL)", ""))


if __name__ == "__main__":
    unittest.main()

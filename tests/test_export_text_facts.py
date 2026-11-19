"""Тесты выгрузки реквизитов из текстов документов и поиска дат, сумм, номеров (без БД)."""
import asyncio
import csv
import json
import os
import tempfile
import unittest
from unittest import mock

from knowledge_store import primary_remarks as pr
from scripts import export_text_facts as ex

TEXT = "Контракт № 03/2024 от 18 марта 2024г. УПД №007 от 05.04.24 на сумму 1 514 970,00 руб., аванс 167400.50"


class FindersTests(unittest.TestCase):
    """Даты, суммы и номера из текста."""

    def test_dates_amounts_numbers(self):
        """Даты нормализуются, суммы с разрядами и копейками находятся, номера — без нулей и регистра."""
        self.assertEqual(pr.find_dates(TEXT), {"18.03.2024", "05.04.2024"})
        self.assertEqual(pr.find_amounts(TEXT), {1514970.0, 167400.5})
        self.assertEqual(pr.find_numbers(TEXT), {"3/2024", "7"})
        self.assertEqual((pr.find_dates(None), pr.find_amounts(""), pr.find_numbers(None)), (set(), set(), set()))

    def test_date_is_not_amount(self):
        """Дата и год не принимаются за сумму."""
        self.assertEqual(pr.find_amounts("от 05.04.2024, 2024 год"), set())


class ExportTests(unittest.TestCase):
    """Строка по документу и файл: текст не выгружается."""

    def test_document_row_has_no_text(self):
        """В строке только реквизиты; документ без текста помечен has_text = False."""
        row = ex.document_row(7, {"id": 1, "filename": "a.pdf", "doc_code": "x", "text_full": TEXT})
        self.assertTrue(row["has_text"])
        self.assertEqual(json.loads(row["dates"]), ["05.04.2024", "18.03.2024"])
        self.assertNotIn("text_full", row)
        self.assertFalse(ex.document_row(7, {"id": 2, "filename": "b.pdf", "text_full": None})["has_text"])

    def test_export_file(self):
        """CSV с заголовком и по строке на документ; чтение ID из файла."""
        async def fake_docs(conn, scope):
            return [{"id": scope.expertise_id * 10, "filename": "a.pdf", "doc_code": "x", "text_full": TEXT},
                    {"id": scope.expertise_id * 10 + 1, "filename": "b.pdf", "doc_code": "y", "text_full": None}]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ex.repo, "get_documents_brief", fake_docs):
            ids_path = os.path.join(tmp, "ids.txt")
            with open(ids_path, "w", encoding="utf-8") as fh:
                fh.write("7, 9\n11")
            self.assertEqual(ex.read_ids(ids_path), [7, 9, 11])
            out = os.path.join(tmp, "out.csv")
            counts = asyncio.run(ex.export(None, "dev", [7, 9], out))
            self.assertEqual(counts, {"expertises": 2, "documents": 4, "with_text": 2})
            with open(out, encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([r["document_id"] for r in rows], ["70", "71", "90", "91"])
            self.assertNotIn("Контракт", open(out, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()

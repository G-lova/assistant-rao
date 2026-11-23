"""Название документа в едином виде и чтение файлов без расширения (печатные формы ЕИС).

Случай — экспертиза с ``risk_monitoring_contract_id`` 8865 (19.10.2026): печатные формы ЕИС без расширения в имени
назывались «Бинарный файл tmp61wjkjn9 (формат .-15406010760260001600001))», вложение XML — «146 Договор
Медипал-ОНКО энтивио.docx», архив УПД — «Извещение об осуществлении закупки».
"""
import asyncio
import importlib.machinery
import os
import sys
import tempfile
import types
import unittest

from evaluate_documents.doc_names import (
    archive_suffix, finalize_document_name, is_unsupported_text, is_weak_name, name_from_filename, normalize_name,
)
from tests.stubs import stubbed_import

EIS_FILE = "Печатная-форма-исполнения-контракта-№-1540601076026000160-(версия-0,-док.-15406010760260001600014)"


def _final(name, filename, fmt=None, text=""):
    """Название после доводки."""
    raw = {"document_name": {"value": name, "confidence": 0.9}}
    finalize_document_name(raw, filename, fmt, text)
    return raw["document_name"]["value"]


class NormalizeNameTest(unittest.TestCase):
    """Даты, «№», регистр, порядок «от … №»."""

    def test_normalize(self):
        """Образцы из ответа эндпоинта приводятся к виду «Вид от ДД.ММ.ГГГГ № номер»."""
        self.assertEqual(normalize_name("СЧЕТ-ФАКТУРА № 5486 ОТ 24 марта 2026 г."), "Счет-фактура от 24.03.2026 № 5486")
        self.assertEqual(normalize_name("Счет на оплату № 4679 от 24 марта 2026 г."), "Счет на оплату от 24.03.2026 № 4679")
        self.assertEqual(normalize_name("ТОВАРНАЯ НАКЛАДНАЯ"), "Товарная накладная")
        self.assertEqual(normalize_name("АКТ ПРИЕМА-ПЕРЕДАЧИ ТОВАРА."), "Акт приема-передачи товара")
        self.assertEqual(normalize_name("Электронный контракт от 2026-03-23 № 146"), "Электронный контракт от 23.03.2026 № 146")
        self.assertEqual(normalize_name("Договор N 146 на поставку"), "Договор № 146 на поставку")
        self.assertEqual(normalize_name("УНИВЕРСАЛЬНЫЙ ПЕРЕДАТОЧНЫЙ ДОКУМЕНТ (УПД)"), "Универсальный передаточный документ (УПД)")
        self.assertEqual(normalize_name("Договор от 23.03.2026 № 146"), "Договор от 23.03.2026 № 146")   # уже в виде

    def test_weak_and_filename(self):
        """Служебная строка и имя временного файла — непригодны; название из имени файла."""
        self.assertTrue(is_weak_name("Бинарный файл tmp61wjkjn9 (формат .-1540)"))
        self.assertTrue(is_weak_name("Документ tmp54bxwfeu"))
        self.assertTrue(is_weak_name(""))
        self.assertFalse(is_weak_name("Договор от 23.03.2026 № 146"))
        self.assertEqual(name_from_filename(EIS_FILE), "Печатная форма исполнения контракта № 1540601076026000160 "
                                                       "(версия 0, док. 15406010760260001600014)")
        self.assertTrue(is_unsupported_text("Бинарный файл «x.dwg» (формат .dwg не поддерживается)"))


class FinalizeTest(unittest.TestCase):
    """Доводка названия: формат файла и замена непригодного названия."""

    def test_formats(self):
        """XML — «(формат XML)», HTML ЕИС — «, печатная форма ЕИС», прочий HTML — «(печатная форма)», архив — состав."""
        self.assertEqual(_final("Электронный контракт от 23.03.2026 № 146", "Электронный-контракт.xml", ".xml"),
                         "Электронный контракт от 23.03.2026 № 146 (формат XML)")
        self.assertEqual(_final("Сведения о заключенном контракте от 23.03.2026 № 146", EIS_FILE, ".html",
                                "Единой информационной системы zakupki.gov.ru"),
                         "Сведения о заключенном контракте от 23.03.2026 № 146, печатная форма ЕИС")
        self.assertEqual(_final("Платежное поручение от 27.03.2026 № 85758", "Платёжное-поручение.html", ".html", "…"),
                         "Платежное поручение от 27.03.2026 № 85758 (печатная форма)")
        text = "=== ON_NSCHFDOPPR.xml ===\n…\n=== card.html ===\n…\n=== ON_NSCHFDOPPR.xml.sig ===\n"
        self.assertEqual(_final("УПД от 24.03.2026", "UPD_20260325.zip", ".zip", text),
                         "УПД от 24.03.2026 (архив XML, HTML и файлов электронной подписи)")
        self.assertEqual(archive_suffix(""), " (архив)")
        self.assertEqual(_final("Договор от 23.03.2026 № 146", "146-Договор.docx", ".docx"), "Договор от 23.03.2026 № 146")
        # формат уже назван моделью — не дублируется
        self.assertEqual(_final("Электронный контракт в формате XML", "a.xml", ".xml"), "Электронный контракт в формате XML")

    def test_weak_replaced(self):
        """«Бинарный файл tmp…» и имя вложения «….docx» — заменяются названием из имени файла."""
        self.assertEqual(_final("Бинарный файл tmp61wjkjn9 (формат .-1540)", EIS_FILE, ".html", "zakupki.gov.ru"),
                         "Печатная форма исполнения контракта № 1540601076026000160 (версия 0, док. 15406010760260001600014)")
        self.assertEqual(_final("146 Договор Медипал-ОНКО энтивио.docx", "Электронный-контракт.xml", ".xml"),
                         "Электронный контракт (формат XML)")
        self.assertEqual(_final(None, "чертёж.dwg", ".bin", "Бинарный файл «чертёж.dwg» (формат .dwg не поддерживается)"),
                         "Чертёж (формат файла не поддерживается)")


class _AsyncFile:
    """Файл с асинхронным интерфейсом (``read`` как корутина) — замена ``aiofiles`` в песочнице."""

    def __init__(self, path, mode="r", encoding=None):
        """Открывает файл обычным ``open``."""
        self._file = open(path, mode) if "b" in mode else open(path, mode, encoding=encoding or "utf-8")

    async def __aenter__(self):
        """Вход в контекст."""
        return self

    async def __aexit__(self, *exc):
        """Закрывает файл."""
        self._file.close()
        return False

    async def read(self, size=-1):
        """Читает файл (ошибки декодирования — как у настоящего aiofiles)."""
        return self._file.read(size)


def _fake_aiofiles():
    """Модуль ``aiofiles`` с ``open`` (со ``__spec__``: stubbed_import не заменит его MagicMock)."""
    module = types.ModuleType("aiofiles")
    module.__spec__ = importlib.machinery.ModuleSpec("aiofiles", None)
    module.open = lambda path, mode="r", encoding=None, **kw: _AsyncFile(path, mode, encoding)
    return module


class RequisitesTest(unittest.TestCase):
    """Реквизиты «от ДД.ММ.ГГГГ № номер» в названии, если модель их не написала."""

    def test_form_header_and_title(self):
        """Товарная накладная: шапка формы «Номер документа | Дата составления» или заголовок «№ … от …»."""
        form = ("Страница 1 (OCR):\nТОВАРНАЯ НАКЛАДНАЯ | Номер документа | Дата составления |\n| | 5486 | 24.03.2026 |\n"
                "Поставщик ООО «Медипал-Онко»")
        self.assertEqual(_final("ТОВАРНАЯ НАКЛАДНАЯ", "ТН_.pdf", ".pdf", form), "Товарная накладная от 24.03.2026 № 5486")
        self.assertEqual(_final("Товарная накладная", "ТН_.pdf", ".pdf", "ТОВАРНАЯ НАКЛАДНАЯ № 5486 от 24.03.2026"),
                         "Товарная накладная от 24.03.2026 № 5486")

    def test_contract_from_extracted(self):
        """Электронный контракт и сведения о заключённом контракте — номер договора и дата заключения из данных."""
        raw = {"contract_number": {"value": "146"}, "dates": [{"value": "2026-03-23", "kind": "signing"}]}

        def named(name, filename, fmt, text=""):
            """Название с извлечёнными данными договора."""
            data = {**raw, "document_name": {"value": name, "confidence": 0.9}}
            finalize_document_name(data, filename, fmt, text)
            return data["document_name"]["value"]

        self.assertEqual(named("146 Договор Медипал-ОНКО энтивио.docx", "Электронный-контракт.xml", ".xml"),
                         "Электронный контракт от 23.03.2026 № 146 (формат XML)")
        self.assertEqual(named("Сведения о заключенном контракте, реестровый номер 1540601076026000160", EIS_FILE,
                               ".html", "zakupki.gov.ru"),
                         "Сведения о заключенном контракте от 23.03.2026 № 146, реестровый номер 1540601076026000160, "
                         "печатная форма ЕИС")
        # у акта и сведений об исполнении свои реквизиты — номер договора им не подставляется
        self.assertEqual(named("Акт приема-передачи товара", "Акт.pdf", ".pdf", "АКТ ПРИЕМА-ПЕРЕДАЧИ ТОВАРА"),
                         "Акт приема-передачи товара")
        self.assertEqual(named("Сведения об исполнении контракта", EIS_FILE, ".html", "zakupki.gov.ru"),
                         "Сведения об исполнении контракта, печатная форма ЕИС")


class MimeTest(unittest.TestCase):
    """Расширение по MIME-типу из БД портала (media.mime_type)."""

    def setUp(self):
        """Модуль file_reader (aiofiles — настоящий или упрощённый)."""
        try:
            import aiofiles  # noqa: F401
        except ImportError:
            sys.modules["aiofiles"] = _fake_aiofiles()
            self.addCleanup(sys.modules.pop, "aiofiles", None)
        ctx = stubbed_import("configs.file_reader")
        self.module = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_extension_and_name(self):
        """text/html → .html; octet-stream и пусто — ничего; понятное расширение в имени не меняется."""
        m = self.module
        self.assertEqual(m.extension_from_mime("text/html; charset=UTF-8"), ".html")
        self.assertEqual(m.extension_from_mime("application/xml"), ".xml")
        self.assertIsNone(m.extension_from_mime("application/octet-stream"))
        self.assertIsNone(m.extension_from_mime(None))
        self.assertEqual(m.name_for_reading(EIS_FILE, "text/html"), EIS_FILE + ".html")
        self.assertEqual(m.name_for_reading("Договор.docx", "text/html"), "Договор.docx")
        self.assertEqual(m.name_for_reading(EIS_FILE, None), EIS_FILE)
        self.assertEqual(m.name_for_reading("чертёж.dwg", "application/pdf"), "чертёж.dwg.pdf")


class MediaMimeMapTest(unittest.TestCase):
    """Карта «id файла → MIME-тип» из запроса экспертизы и id из ссылки."""

    def test_map_and_id(self):
        """JSON-объекты колонки file_mime объединяются; id берётся из …/media/<id>/…."""
        import pandas as pd
        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as module:
            df = pd.DataFrame({"file_mime": ['{"54600": "text/html", "54601": "application/pdf"}', None,
                                             '{"54613": "text/html", "1": null}']})
            self.assertEqual(module.media_mime_map(df), {"54600": "text/html", "54601": "application/pdf",
                                                         "54613": "text/html"})
            self.assertEqual(module.media_mime_map(pd.DataFrame({"x": [1]})), {})
            self.assertEqual(module.media_id_from_url("https://expert.rusacademedu.ru/media/54600/Печатная"), "54600")
            self.assertEqual(module.media_id_from_url("https://x.ru/api/media/54613"), "54613")
            self.assertIsNone(module.media_id_from_url("https://zakupki.gov.ru/epz/order"))


class FileReaderFormatTest(unittest.TestCase):
    """Расширение из имени и формат по содержимому."""

    def setUp(self):
        """FileReader без инициализации тяжёлых зависимостей; aiofiles — настоящий или упрощённый."""
        try:
            import aiofiles  # noqa: F401
        except ImportError:
            sys.modules["aiofiles"] = _fake_aiofiles()
            self.addCleanup(sys.modules.pop, "aiofiles", None)
        ctx = stubbed_import("configs.file_reader")
        self.module = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.reader = self.module.FileReader.__new__(self.module.FileReader)
        self.dir = tempfile.mkdtemp()

    def _file(self, name, data: bytes):
        """Временный файл с содержимым."""
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_get_extension(self):
        """«.-15406010760260001600001)» — не расширение; обычные и составные — как раньше."""
        self.assertEqual(self.reader.get_extension(EIS_FILE), "")
        self.assertEqual(self.reader.get_extension("Печатная-форма-контракта-(версия-0,-док.-1540)"), "")
        self.assertEqual(self.reader.get_extension("Договор.DOCX"), ".docx")
        self.assertEqual(self.reader.get_extension("архив.tar.gz"), ".tar.gz")
        self.assertEqual(self.reader.get_extension("без расширения"), "")

    def test_source_format_sniff(self):
        """HTML (в т.ч. с BOM и windows-1251), XML, PDF — по содержимому; расширение имени — если оно знакомое."""
        html = self._file("eis", "﻿<!DOCTYPE html><html><body>Сведения</body></html>".encode("utf-8"))
        html_1251 = self._file("eis2", "<html><head></head><body>Контракт</body></html>".encode("cp1251"))
        xml = self._file("x", b"  <?xml version='1.0'?><root/>")
        pdf = self._file("p", b"%PDF-1.7 ...")
        run = asyncio.run
        self.assertEqual(run(self.reader.source_format(html, EIS_FILE)), ".html")
        self.assertEqual(run(self.reader.source_format(html_1251, EIS_FILE)), ".html")
        self.assertEqual(run(self.reader.source_format(xml, "Электронный-контракт")), ".xml")
        self.assertEqual(run(self.reader.source_format(pdf, "скан")), ".pdf")
        self.assertEqual(run(self.reader.source_format(pdf, "Договор.docx")), ".docx")

    def test_read_eis_print_form(self):
        """Печатная форма ЕИС без расширения читается как HTML, а не «Бинарный файл»."""
        path = self._file("tmp61wjkjn9", "<html><body><p>Сведения о заключенном контракте</p>"
                                         "<p>№ 146 от 23.03.2026</p></body></html>".encode("utf-8"))
        text = asyncio.run(self.reader.read_file(path, EIS_FILE))
        self.assertIn("Сведения о заключенном контракте", text)
        self.assertNotIn("Бинарный файл", text)

    def test_unsupported_message_uses_original_name(self):
        """Неподдерживаемый формат: в строке — исходное имя файла, а не имя временного файла."""
        path = self._file("tmpabcdef12", bytes(range(256)) * 4)
        text = asyncio.run(self.reader.read_file(path, "чертёж.dwg"))
        self.assertTrue(text.startswith("Бинарный файл «чертёж.dwg"), text)
        self.assertNotIn("tmpabcdef12", text)


if __name__ == "__main__":
    unittest.main()

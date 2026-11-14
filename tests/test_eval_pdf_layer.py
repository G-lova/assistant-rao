"""Чтение PDF в экспертизе (``FileReader.read_pdf_file``): текстовый слой вместо OCR, постраничный рендер.

Страница 1 — текст без изображений (берётся из слоя), страница 2 — текст и растровое изображение (подпись/печать:
распознаётся моделью), страница 3 — пустая («скан» без слоя: распознаётся моделью).
"""
import asyncio
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def make_pdf(path: str) -> None:
    """Создаёт трёхстраничный PDF (см. модуль)."""
    from PIL import Image
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(TTFont("DejaVu", FONT))
    img = os.path.join(os.path.dirname(path), "stamp.png")
    Image.new("RGB", (60, 60), "blue").save(img)
    c = canvas.Canvas(path)
    c.setFont("DejaVu", 11)
    c.drawString(50, 800, "Контракт на поставку товара для обеспечения государственных нужд")
    c.showPage()
    c.setFont("DejaVu", 11)
    c.drawString(50, 800, "Акт приёмки выполненных работ по контракту и подписи сторон")
    c.drawImage(img, 50, 600, 60, 60)
    c.showPage()
    c.showPage()
    c.save()


class EvalPdfLayerTest(unittest.TestCase):
    """``read_pdf_file``: какие страницы идут в OCR и как размечается результат."""

    @classmethod
    def setUpClass(cls):
        try:
            import pdf2image  # noqa: F401
            import reportlab  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("нет reportlab / pdf2image")
        if not os.path.exists(FONT) or not shutil.which("pdftoppm"):
            raise unittest.SkipTest("нет шрифта DejaVuSans или poppler")
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "doc.pdf")
        make_pdf(cls.path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, ocr=None, **flags):
        """Читает тестовый PDF ``FileReader.read_pdf_file`` без настоящего OCR.

        Args:
            ocr: Имитация ``ocr_image_with_qwen_vl``; по умолчанию отвечает JSON с текстом.
            **flags: Значения настроек (``EVAL_PDF_TEXT_LAYER``, ``EVAL_PDF_OCR_IMAGE_PAGES``) на время чтения.

        Returns:
            Tuple[str, list]: Текст и имена изображений, переданных в OCR.
        """
        from tests.stubs import stubbed_import

        seen = []

        async def default_ocr(image_path, name):
            """Имитация модели OCR."""
            self.assertTrue(os.path.exists(image_path))
            self.assertTrue(image_path.endswith(".jpg"))
            seen.append(os.path.basename(image_path))
            return '{"raw_text": "распознано", "visual_notes": "печать"}'

        with stubbed_import("configs.file_reader") as fr:
            import configs.settings as settings

            reader = fr.FileReader.__new__(fr.FileReader)
            reader.semaphore = asyncio.Semaphore(2)
            reader.ocr_processor = types.SimpleNamespace(ocr_image_with_qwen_vl=ocr or default_ocr)
            flags = {"EVAL_PDF_TEXT_LAYER": True, "EVAL_PDF_OCR_IMAGE_PAGES": True, **flags}
            with mock.patch.multiple(settings, create=True, **flags):
                text = asyncio.run(reader.read_pdf_file(self.path, "doc.pdf"))
        return text, seen

    def test_layer_and_ocr(self):
        """Страница 1 — из слоя, страницы 2 (изображение) и 3 (нет слоя) — OCR."""
        text, seen = self.read()
        self.assertIn("Page 1:\nСтраница 1: Контракт на поставку", text)
        self.assertIn('Page 2:\nСтраница 2 (OCR): {"raw_text"', text)
        self.assertIn("Page 3:\nСтраница 3 (OCR):", text)
        self.assertEqual(len(seen), 2)

    def test_pages_split(self):
        """Разметку понимает ``knowledge_store.pages.split_pages``."""
        from knowledge_store.pages import split_pages

        text, _ = self.read()
        numbers = sorted({p.number for p in split_pages(text) if p.text.strip()})
        self.assertEqual(numbers, [1, 2, 3])

    def test_image_pages_from_layer_when_allowed(self):
        """``EVAL_PDF_OCR_IMAGE_PAGES = False`` — страница с изображением тоже из слоя."""
        text, seen = self.read(EVAL_PDF_OCR_IMAGE_PAGES=False)
        self.assertIn("Страница 2: Акт приёмки", text)
        self.assertEqual(len(seen), 1)

    def test_layer_disabled(self):
        """``EVAL_PDF_TEXT_LAYER = False`` — как раньше, OCR каждой страницы."""
        text, seen = self.read(EVAL_PDF_TEXT_LAYER=False)
        self.assertEqual(len(seen), 3)
        self.assertNotIn("Страница 1: Контракт", text)

    def test_xobject_image_in_form(self):
        """Изображение внутри формы (``/Form`` → ``/Image``) находится; формы без изображений — нет."""
        from tests.stubs import stubbed_import

        with_image = {"/XObject": {"/Fm1": {"/Subtype": "/Form",
                                            "/Resources": {"/XObject": {"/Im1": {"/Subtype": "/Image"}}}}}}
        without = {"/XObject": {"/Fm1": {"/Subtype": "/Form", "/Resources": {}}}, "/Font": {}}
        with stubbed_import("configs.file_reader") as fr:
            self.assertTrue(fr._xobjects_have_image(with_image))
            self.assertFalse(fr._xobjects_have_image(without))
            self.assertFalse(fr._xobjects_have_image(None))

    def test_ocr_error_marked(self):
        """Сбой OCR страницы не роняет документ: страница помечается ошибкой."""
        async def fail(image_path, name):
            """Имитация сбоя модели."""
            raise RuntimeError("503")

        text, _ = self.read(ocr=fail)
        self.assertIn("Страница 3 (OCR): [Ошибка: 503]", text)
        self.assertIn("Страница 1: Контракт", text)


if __name__ == "__main__":
    unittest.main()

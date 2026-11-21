"""Прод, 18.11.2026: блокировка ЕИС, служебные файлы и огромные архивы.

* ЕИС отвечал 403 «подозрительная активность» на все файлы подряд — каждый файл тратил попытку. Теперь
  ``SourceBlocked``: попытка не засчитывается, скачивание из ЕИС приостанавливается (``RM_EIS_BLOCK_PAUSE_SEC``).
* Одна и та же прошлая редакция (972335) скачивалась для десятков печатных форм подряд — неудача запоминается.
* Архив проектной документации: временные файлы Word ``~$*.docx`` давали ошибки с трассировкой; сотни смет и
  картинок читались больше 30 мин — в РМ архив читается с пределами (``RM_ARCHIVE_*``).
"""
import asyncio
import os
import tempfile
import types
import unittest
import zipfile
from unittest import mock


def analyzer(download):
    """``AIFileAnalyzer`` без конструктора: ``parser._download_http_file`` подменён."""
    from risk_monitoring import ai_file_analyzer as m

    a = m.AIFileAnalyzer.__new__(m.AIFileAnalyzer)
    a.parser = types.SimpleNamespace(_download_http_file=download)
    return a


class EisBlockTest(unittest.TestCase):
    """``SourceBlocked``: пауза скачивания из ЕИС, попытка не засчитывается."""

    def setUp(self):
        from risk_monitoring import ai_file_analyzer as m

        m._EIS_PAUSED_UNTIL = 0.0
        self.m = m

    tearDown = setUp

    def test_block_pauses_eis(self):
        calls = []

        async def download(url, **kw):
            calls.append(url)
            return {"status": "error", "blocked": True,
                    "error": "HTTP 403: ЕИС заблокировал запросы («подозрительная активность»)"}

        a = analyzer(download)
        eis = "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=1"
        with self.assertRaises(self.m.SourceBlocked):
            asyncio.run(a._load_document(1, eis, "a.pdf"))
        self.assertGreater(self.m._eis_pause_left(), 0)
        with self.assertRaises(self.m.SourceBlocked):          # во время паузы ЕИС не запрашивается
            asyncio.run(a._load_document(2, eis, "b.pdf"))
        self.assertEqual(len(calls), 1)

    def test_storage_403_then_eis_blocked_keeps_type(self):
        """Хранилище 403 (nginx), ЕИС — блокировка: ошибка остаётся ``SourceBlocked``."""
        async def download(url, **kw):
            if "zakupki" in url:
                return {"status": "error", "blocked": True, "error": "ЕИС: подозрительная активность"}
            return {"status": "error", "error": "HTTP 403: не удалось скачать файл"}

        a = analyzer(download)
        row = {"url": "https://expert.example/storage/x.pdf", "source_url": "https://zakupki.gov.ru/x"}
        with self.assertRaises(self.m.SourceBlocked) as cm:
            asyncio.run(a._load_with_fallback(5, row, "x.pdf"))
        self.assertIn("хранилище", str(cm.exception))

    def test_attempt_not_counted(self):
        a = self.m.AIFileAnalyzer.__new__(self.m.AIFileAnalyzer)
        pushed = []

        async def analyze_file(row, previous, attempt):
            raise self.m.SourceBlocked("ЕИС: подозрительная активность")

        async def push_failure(file_id, attempt, error):
            pushed.append((file_id, attempt, error))
            return {"status": "failed"}

        a.analyze_file, a._push_failure = analyze_file, push_failure
        asyncio.run(a._attempt({"id": 7}, None, 2))
        self.assertEqual(pushed[0][1], 1)                       # попытка 2 → записана как 1
        self.assertTrue(pushed[0][2].startswith("eis_blocked"))

    def test_storage_403_is_still_counted(self):
        """Обычный 403 хранилища (не блокировка ЕИС) — как раньше, ``FileUnavailable`` с тратой попытки."""
        async def download(url, **kw):
            return {"status": "error", "error": "HTTP 403: не удалось скачать файл"}

        a = analyzer(download)
        with self.assertRaises(self.m.FileUnavailable) as cm:
            asyncio.run(a._load_document(1, "https://expert.example/storage/x.pdf", "x.pdf"))
        self.assertNotIsInstance(cm.exception, self.m.SourceBlocked)
        self.assertEqual(self.m._eis_pause_left(), 0)


class PreviousCacheTest(unittest.TestCase):
    """Прошлая редакция, не скачавшаяся в этом прогоне, не скачивается снова."""

    def test_failed_previous_not_reloaded(self):
        from risk_monitoring import ai_file_analyzer as m

        a = m.AIFileAnalyzer.__new__(m.AIFileAnalyzer)
        a.env = "prod"
        calls = []

        async def get_link(env, fid):
            return None

        async def load(file_id, url, file_name, **kw):
            calls.append(file_id)
            raise m.FileUnavailable("HTTP 403: не удалось скачать файл")

        a.repo = types.SimpleNamespace(get_link=get_link)
        a._load_document = load
        prev = {"id": 972335, "url": "https://expert.example/storage/p.html", "file_name": "p.html"}
        for _ in range(3):
            out = asyncio.run(a._load_previous(prev, "sha", "x.html"))
            self.assertIn("Предыдущая версия недоступна", out["error"])
        self.assertEqual(calls, [972335])


class ArchiveTest(unittest.TestCase):
    """``FileReader.process_archive``: служебные файлы и пределы чтения."""

    def reader(self, limits=None):
        from tests.stubs import stubbed_import

        with stubbed_import("configs.file_reader") as module:
            r = module.FileReader.__new__(module.FileReader)
            r.archive_limits = limits
            seen = []

            async def read_file(path, original_filename=None):
                seen.append(original_filename)
                if original_filename.startswith("~$") or "/~$" in original_filename:
                    raise ValueError("Package not found")
                return "т" * 100

            r.read_file = read_file
            return module, r, seen

    def make_zip(self, names):
        path = os.path.join(tempfile.mkdtemp(), "a.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for n in names:
                zf.writestr(n, b"x")
        return path

    def test_junk(self):
        module, _, _ = self.reader()
        self.assertTrue(module.is_archive_junk("ПД/Формат разработки/~$5-092-ООС1.3.docx"))
        self.assertTrue(module.is_archive_junk("__MACOSX/ПД/._Том1.pdf"))
        self.assertTrue(module.is_archive_junk("Т-15/Thumbs.db"))
        self.assertFalse(module.is_archive_junk("ПД/215-092-ООС1.3 изм.1.docx"))

    def test_junk_skipped_without_limits(self):
        module, r, seen = self.reader()
        path = self.make_zip(["ПД/~$Том.docx", "ПД/Том.docx", "ПД/Thumbs.db"])
        text = asyncio.run(r.process_archive(path, ".zip"))
        self.assertEqual(seen, ["ПД/Том.docx"])
        self.assertNotIn("Ошибка чтения", text)

    def test_limits_order_and_list(self):
        names = [f"Сметы/ЛСР-{i:02d}.xlsx" for i in range(10)] + [f"Чертежи/{i}.jpg" for i in range(5)] + \
                ["ТЗ.docx", "Контракт.pdf"]
        module, r, seen = self.reader({"max_files": 6, "max_images": 1, "max_chars": 0, "concurrency": 2})
        text = asyncio.run(r.process_archive(self.make_zip(names), ".zip"))
        self.assertEqual(seen[:2], ["Контракт.pdf", "ТЗ.docx"])        # документы — первыми
        self.assertEqual(len(seen), 6)
        self.assertEqual(sum(n.endswith(".jpg") for n in seen), 0)      # таблицы раньше картинок, предел файлов
        self.assertIn("Не прочитано", text)
        self.assertIn("прочитано 6 из 17", text)

    def test_char_limit(self):
        names = [f"Д{i}.docx" for i in range(10)]
        module, r, seen = self.reader({"max_files": 10, "max_images": 0, "max_chars": 250, "concurrency": 2})
        asyncio.run(r.process_archive(self.make_zip(names), ".zip"))
        self.assertEqual(len(seen), 4)                                   # 2 пачки по 2 файла → больше 250 символов


if __name__ == "__main__":
    unittest.main()


class SafeExtractTest(unittest.TestCase):
    """``FileReader.extract_archive``: длинные имена, «..», cp866 в zip (прод, 20.11.2026: Errno 36)."""

    LONG = ("33. Аппаратно-программный комплекс с технологиями искусственного интеллекта для отработки навыков "
            "проведения медицинской консультации с виртуальным пациентом.pdf")

    def module(self):
        from tests.stubs import stubbed_import

        with stubbed_import("configs.file_reader") as module:
            return module

    def test_long_name_shortened_and_read(self):
        module = self.module()
        r = module.FileReader.__new__(module.FileReader)
        r.archive_limits = None
        seen = []

        async def read_file(path, original_filename=None):
            seen.append((os.path.basename(path), original_filename))
            return "текст"

        r.read_file = read_file
        path = os.path.join(tempfile.mkdtemp(), "a.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Документы/" + self.LONG, b"%PDF")
            zf.writestr("Документы/ТЗ.docx", b"x")
        self.assertGreater(len(self.LONG.encode("utf-8")), 255)
        text = asyncio.run(r.process_archive(path, ".zip"))
        saved = dict(seen)
        short = next(n for n in saved if n.startswith("33."))
        self.assertLessEqual(len(short.encode("utf-8")), 240)
        self.assertTrue(short.endswith(".pdf"))
        self.assertEqual(saved[short], "Документы/" + self.LONG)        # в тексте — исходное имя
        self.assertIn("=== Документы/" + self.LONG + " ===", text)

    def test_traversal_and_collisions(self):
        module = self.module()
        out = tempfile.mkdtemp()
        self.assertEqual(module.safe_member_path("../../etc/passwd", out), os.path.join("etc", "passwd"))
        self.assertEqual(module.safe_member_path("/abs/a.txt", out), os.path.join("abs", "a.txt"))
        self.assertEqual(module.safe_member_path("..", out), "")
        a = module.safe_member_path("x/" + self.LONG, out)
        os.makedirs(os.path.join(out, "x"))
        open(os.path.join(out, a), "w").close()
        b = module.safe_member_path("x/" + self.LONG, out)
        self.assertNotEqual(a, b)

    def test_zip_cp866_and_tar(self):
        import tarfile

        module = self.module()
        r = module.FileReader.__new__(module.FileReader)
        d = tempfile.mkdtemp()
        zpath = os.path.join(d, "w.zip")
        # имя в cp866 без флага UTF-8, как пишет Windows: ASCII-имя той же длины, затем байты имени подменяются
        cp866 = "ТЗ.docx".encode("cp866")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("TZ.docx", b"x")
        raw = open(zpath, "rb").read().replace(b"TZ.docx", cp866)
        open(zpath, "wb").write(raw)
        out = tempfile.mkdtemp()
        asyncio.run(r.extract_archive(zpath, ".zip", out))
        self.assertEqual(os.listdir(out), ["ТЗ.docx"])
        tpath = os.path.join(d, "t.tar")
        src = os.path.join(d, "f.txt")
        open(src, "w").write("x")
        with tarfile.open(tpath, "w") as tf:
            tf.add(src, arcname="../evil.txt")
        out = tempfile.mkdtemp()
        asyncio.run(r.extract_archive(tpath, ".tar", out))
        self.assertEqual(os.listdir(out), ["evil.txt"])                   # не выше каталога распаковки


class NotTextAndOcrLimitTest(unittest.TestCase):
    """Прод, 21.11.2026: архив чертежей «РД (КЖ …).rar» (.dwg, .dwl, .tmp) и сканы в «ПСД (Раздел 9-10).zip» (837 с)."""

    def module(self):
        from tests.stubs import stubbed_import

        with stubbed_import("configs.file_reader") as module:
            return module

    def test_drawings_listed_not_read(self):
        module = self.module()
        r = module.FileReader.__new__(module.FileReader)
        r.archive_limits = None
        seen = []

        async def read_file(path, original_filename=None):
            seen.append(original_filename)
            return "текст"

        r.read_file = read_file
        path = os.path.join(tempfile.mkdtemp(), "РД.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for n in ("лл. 1-3 Общие данные.dwg", "КЖ.И-КР23,КР24.dwl2", "sav4F79.tmp", "Пояснительная записка.pdf",
                      "README"):
                zf.writestr(n, b"x")
        text = asyncio.run(r.process_archive(path, ".zip"))
        self.assertEqual(sorted(seen), ["README", "Пояснительная записка.pdf"])   # без расширения — читается
        self.assertIn("Нетекстовые файлы", text)
        self.assertIn("лл. 1-3 Общие данные.dwg", text)
        self.assertTrue(module.is_readable_name("а.docx"))
        self.assertFalse(module.is_readable_name("а.dwg"))

    def test_ocr_page_limit(self):
        module = self.module()
        r = module.FileReader.__new__(module.FileReader)
        r.semaphore = asyncio.Semaphore(4)
        r.max_ocr_pages = 3
        r.ocr_tail_pages = 0
        ocr = []

        async def ocr_image(path, name):
            ocr.append(path)
            return "распознано"

        r.ocr_processor = types.SimpleNamespace(ocr_image_with_qwen_vl=ocr_image)
        with mock.patch.object(module, "pdf_layer_pages", return_value=[]), \
                mock.patch.object(module, "pdf_page_count", return_value=10), \
                mock.patch.object(module, "pdf_text_layer_enabled", return_value=True), \
                mock.patch.object(module, "convert_from_path",
                                  side_effect=lambda *a, **k: [os.path.join(k["output_folder"], "p.jpg")]):
            text = asyncio.run(r.read_pdf_file("/tmp/скан.pdf", "скан.pdf"))
        self.assertEqual(len(ocr), 3)
        self.assertEqual(text.count("предел OCR 3"), 7)
        self.assertIn("Страница 4: [не распознана", text)
        r.ocr_tail_pages = 1   # первые 2 и последняя (подписи, реквизиты)
        ocr.clear()
        with mock.patch.object(module, "pdf_layer_pages", return_value=[]), \
                mock.patch.object(module, "pdf_page_count", return_value=10), \
                mock.patch.object(module, "pdf_text_layer_enabled", return_value=True), \
                mock.patch.object(module, "convert_from_path",
                                  side_effect=lambda *a, **k: [os.path.join(k["output_folder"], "p.jpg")]):
            text = asyncio.run(r.read_pdf_file("/tmp/скан.pdf", "скан.pdf"))
        self.assertEqual(len(ocr), 3)
        self.assertIn("Страница 10 (OCR): распознано", text)
        self.assertIn("Страница 3: [не распознана", text)
        self.assertNotIn("Страница 2: [не распознана", text)
        r.max_ocr_pages = None   # экспертиза — все страницы
        ocr.clear()
        with mock.patch.object(module, "pdf_layer_pages", return_value=[]), \
                mock.patch.object(module, "pdf_page_count", return_value=10), \
                mock.patch.object(module, "pdf_text_layer_enabled", return_value=True), \
                mock.patch.object(module, "convert_from_path",
                                  side_effect=lambda *a, **k: [os.path.join(k["output_folder"], "p.jpg")]):
            asyncio.run(r.read_pdf_file("/tmp/скан.pdf", "скан.pdf"))
        self.assertEqual(len(ocr), 10)

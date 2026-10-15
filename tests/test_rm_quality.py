"""Тесты контроля качества анализа файлов риск-мониторинга (по итогам прогона файлов 1096/1097):
фильтр OCR, вид документа и печатные формы, допустимые коды, очистка паспорта, фильтры рисков,
переход на схему в промпте, если сервер не поддерживает guided_json.
"""
import asyncio
import json
import types
import unittest

from risk_monitoring import doc_kind, text_quality
from risk_monitoring.document_profiler import find_headings, is_heading, sanitize_profile
from risk_monitoring.document_risk_analyzer import brand_like, self_denial


class TextQualityTest(unittest.TestCase):
    """``text_quality``: циклы OCR, вердикт, сверка реквизитов."""

    def test_loop_is_bad(self):
        """Зациклившийся OCR («2,44» × 400) — текст негоден."""
        text = "Обоснование начальной цены контракта. " + "2,44 " * 400
        q = text_quality.assess(text)
        self.assertEqual(q["status"], "bad")
        self.assertIn("[повтор ×400]", q["text"])

    def test_short_repeat_is_ok(self):
        """Обычный текст с несколькими повторами фразы — ``ok``."""
        text = ("Поставщик обязан поставить товар в срок. " * 3) + "Требования к качеству товара указаны в приложении. " * 5
        self.assertEqual(text_quality.assess(text)["status"], "ok")

    def test_suspect(self):
        """Небольшой цикл в длинном тексте — ``suspect`` и очищенный текст."""
        body = "Описание объекта закупки содержит характеристики товара и требования к упаковке. " * 30
        q = text_quality.assess(body + " бананы" * 12)
        self.assertEqual(q["status"], "suspect")
        self.assertEqual(q["loops"], 1)

    def test_clean_value(self):
        """Строки в ai_analysis очищаются от циклов и обрезаются."""
        out = text_quality.clean_value({"a": ["x " * 50], "b": "я" * 3000})
        self.assertIn("[повтор", out["a"][0])
        self.assertTrue(out["b"].endswith("…"))

    def test_verify_identifiers(self):
        """ИНН, которого нет в тексте, снимается; найденный — остаётся."""
        raw = {"organizations": [{"name": "А", "inn": "7801000000"}, {"name": "Б", "inn": "7702 123456"}],
               "contract_number": {"value": "0373100000124000001"}}
        cleaned, removed = text_quality.verify_identifiers(raw, "ИНН 7702123456, контракт 0373100000124000001")
        self.assertIsNone(cleaned["organizations"][0]["inn"])
        self.assertEqual(cleaned["organizations"][1]["inn"], "7702 123456")
        self.assertEqual(cleaned["contract_number"]["value"], "0373100000124000001")
        self.assertEqual(len(removed), 1)


class DocKindTest(unittest.TestCase):
    """``doc_kind``: печатные формы, тип по имени, итоговый тип, допустимые коды."""

    def test_print_form_groups(self):
        """Печатная форма любого XML: группа по тегу источника."""
        self.assertTrue(doc_kind.is_print_form({"file_name": "print_form_123.html"}))
        notice = doc_kind.print_form_kind({"source_xml_tag": "epNotificationEF2020", "eis_version": 1})
        self.assertEqual(notice["group"], "notice")
        self.assertEqual(notice["detected_type"], "docIzvejenieFiles")
        self.assertIn("(изменение)", notice["type_decode"])
        contract = doc_kind.print_form_kind({"source_xml_tag": "contract"})
        self.assertEqual(contract["group"], "contract")
        self.assertEqual(contract["detected_type"], doc_kind.PRINT_FORM_TYPE)
        self.assertEqual(doc_kind.print_form_kind({"source_xml_tag": "epProtocolEF2020Final"})["group"], "protocol")
        self.assertEqual(doc_kind.print_form_kind({"source_xml_tag": "contractProcedure"})["group"], "execution")

    def test_type_by_name(self):
        """Тип по имени файла."""
        cases = {
            "Обоснование начальной (максимальной) цены контракта.pdf": "docObosnNMCKFiles",
            "2. Описание объекта закупки.docx": "docOpusObjectZacupFiles",
            "Проект контракта.docx": "docProjContractFiles",
            "Требования к содержанию, составу заявки.docx": "docTrebContentRequestFiles",
            "scan_0001.pdf": None,
        }
        for name, expected in cases.items():
            self.assertEqual(doc_kind.type_by_name(name), expected, name)

    def test_resolve_type(self):
        """Неуверенный детектор уступает имени файла; уверенный — нет."""
        allowed = ["docIzvejenieFiles", "docObosnNMCKFiles"]
        weak = {"detected_type": "docIzvejenieFiles", "confidence": 0.6}
        strong = {"detected_type": "docIzvejenieFiles", "confidence": 0.95}
        self.assertEqual(doc_kind.resolve_type(weak, "docObosnNMCKFiles", allowed), ("docObosnNMCKFiles", "file_name"))
        self.assertEqual(doc_kind.resolve_type(strong, "docObosnNMCKFiles", allowed), ("docIzvejenieFiles", "detector"))
        self.assertEqual(doc_kind.resolve_type({"detected_type": "zzz"}, None, allowed, "docX"), ("docX", "document_name"))

    def test_allowed_codes(self):
        """Обоснование НМЦК — без брендов; печатная форма протокола — без поиска рисков."""
        self.assertNotIn("DOC-002", doc_kind.allowed_codes("docObosnNMCKFiles"))
        self.assertEqual(doc_kind.allowed_codes(doc_kind.PRINT_FORM_TYPE, "protocol"), [])
        notice_print = doc_kind.allowed_codes("docIzvejenieFiles", "notice")
        self.assertIn("DOC-002", notice_print)
        self.assertNotIn("FIN-004", notice_print)
        self.assertNotIn("DOC-003", notice_print)

    def test_print_form_by_russian_name(self):
        """«Печатная форма извещения № … (версия 1).html» с типом ``epNotification`` — извещение (файл 37043)."""
        row = {"file_name": "Печатная форма извещения № 0373100029526000124 (версия 1).html",
               "xml_source_type": "epNotification", "owner_type": "notice", "eis_version": 3}
        self.assertTrue(doc_kind.is_print_form(row))
        kind = doc_kind.print_form_kind(row)
        self.assertEqual(kind["group"], "notice")
        self.assertEqual(kind["type_decode"], "Печатная форма ЕИС: извещение о закупке (изменение)")
        self.assertTrue(doc_kind.allowed_codes(kind["detected_type"], kind["group"]))

    def test_xml_group_fallback(self):
        """Неизвестный тег — группа по владельцу файла."""
        self.assertEqual(doc_kind.xml_group("", "contract"), "contract")
        self.assertEqual(doc_kind.xml_group("somethingNew", None), "other")

    def test_doc_type_block_keeps_source(self):
        """``doc_type`` в паспорте сохраняет ``type_source`` и ``print_form``."""
        from risk_monitoring.passport import doc_type_block

        block = doc_type_block({"detected_type": "docIzvejenieFiles", "type_source": "print_form",
                                "print_form": {"group": "notice"}}, "Печатная форма")
        self.assertEqual(block["type_source"], "print_form")
        self.assertEqual(block["print_form"]["group"], "notice")


class ProfileSanitizeTest(unittest.TestCase):
    """Очистка AI-паспорта: заголовки и стандарты."""

    def test_headings(self):
        """Строки таблиц и ссылки на закон — не заголовки."""
        self.assertFalse(is_heading("0.00 Итого"))
        self.assertFalse(is_heading("95 Закона № 44-ФЗ."))
        self.assertTrue(is_heading("1. Общие положения контракта"))
        self.assertEqual(find_headings("1. Общие положения контракта\n0.5 Итого\n"), ["1. Общие положения контракта"])

    def test_standards(self):
        """В стандартах остаются ГОСТ/СП/ТР, постановления правительства — нет."""
        p = sanitize_profile({"main_sections": ["1. Предмет контракта", "0,00 руб"],
                              "technical_objects": {"standards": ["ГОСТ 12345-2010", "ПП 1875"]}})
        self.assertEqual(p["main_sections"], ["1. Предмет контракта"])
        self.assertEqual(p["technical_objects"]["standards"], ["ГОСТ 12345-2010"])


class RiskFilterTest(unittest.TestCase):
    """Фильтры рисков модели."""

    def test_self_denial(self):
        """Риск, описание которого говорит «соответствует законодательству», отбрасывается."""
        self.assertTrue(self_denial({"explanation": "Размер обеспечения соответствует законодательству"}))
        self.assertFalse(self_denial({"explanation": "Размер обеспечения превышает 30 % НМЦК"}))

    def test_brand_like(self):
        """Идентификатор вещества / номер ЕИС — не бренд; торговая марка — бренд."""
        self.assertFalse(brand_like("Идентификатор: 0373100000124000001"))
        self.assertTrue(brand_like("картридж HP 85A или эквивалент не допускается"))


class GuidedFallbackTest(unittest.TestCase):
    """``LLMJson``: сервер без guided_json → схема в системном промпте."""

    def test_fallback(self):
        """Первая попытка 400 «guided_json» → вторая без extra_body и со схемой в промпте."""
        from risk_monitoring.llm_json import LLMJson

        calls = []

        class Err(Exception):
            """Ошибка 400 сервера модели."""
            status_code = 400

        async def create(**kw):
            """Имитация ``chat.completions.create``."""
            calls.append(kw)
            if kw["extra_body"]:
                raise Err("unexpected field guided_json")
            msg = types.SimpleNamespace(content='{"ok": true}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        llm = LLMJson(client, "m", rate=1000)
        out = asyncio.run(llm.ask("sys", "user", {"type": "object"}))
        self.assertEqual(out, {"ok": True})
        self.assertFalse(llm.guided)
        self.assertIn("строго один JSON", calls[-1]["messages"][0]["content"])


class PdfTextTest(unittest.TestCase):
    """``pdf_text``: текстовый слой вместо OCR, OCR только для страниц без слоя, повтор при цикле."""

    @classmethod
    def setUpClass(cls):
        """Готовит PDF: страница с текстом (кириллица) и пустая страница (как скан)."""
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.pdfgen import canvas
            import pdf2image  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("нет reportlab / pdf2image")
        import os
        import tempfile

        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        if not os.path.exists(font):
            raise unittest.SkipTest("нет шрифта DejaVuSans")
        pdfmetrics.registerFont(TTFont("DejaVu", font))
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "nmck.pdf")
        c = canvas.Canvas(cls.path)
        c.setFont("DejaVu", 11)
        for i, line in enumerate(["Обоснование начальной (максимальной) цены контракта",
                                  "Метод сопоставимых рыночных цен (анализа рынка)",
                                  "Коммерческое предложение № 1: 2,44 руб. за единицу товара"]):
            c.drawString(50, 800 - 20 * i, line)
        c.showPage()
        c.showPage()  # пустая страница — «скан» без текстового слоя
        c.save()

    def test_layer_page_ok(self):
        """Кракозябры и (cid:NN) — слой непригоден."""
        from risk_monitoring.pdf_text import layer_page_ok

        self.assertTrue(layer_page_ok("Обоснование начальной максимальной цены контракта методом анализа рынка"))
        self.assertFalse(layer_page_ok("(cid:12)(cid:15) " * 20))
        self.assertFalse(layer_page_ok("ÐžÐ±Ð¾ÑÐ½Ð¾Ð²Ð°Ð½Ð¸Ðµ ÐÐœÐ¦Ðš " * 5))

    def test_layer_and_ocr_retry(self):
        """Страница 1 — из слоя; страница 2 — OCR: первый ответ зациклен, повтор со штрафом даёт текст."""
        from risk_monitoring import pdf_text

        calls = []

        async def create(**kw):
            """Имитация модели OCR: цикл при слабом штрафе, нормальный текст при сильном."""
            calls.append(kw["extra_body"]["repetition_penalty"])
            text = "2,44 " * 300 if kw["extra_body"]["repetition_penalty"] < 1.1 else "Подпись заказчика, печать"
            msg = types.SimpleNamespace(content=json.dumps({"raw_text": text}))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        proc = types.SimpleNamespace(
            client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))),
            model="vl", ocr_prompt="OCR", OCR_SCHEMA={"properties": {"raw_text": {}}, "required": ["readability"]})
        res = asyncio.run(pdf_text.read_pdf(self.path, "nmck.pdf", proc))
        self.assertEqual((res["layer_pages"], res["ocr_pages"]), (1, 1))
        self.assertIn("Страница 1:\nОбоснование начальной", res["text"])
        self.assertIn("Страница 2 (OCR):\nПодпись заказчика", res["text"])
        self.assertEqual(calls, [1.05, 1.15])
        self.assertEqual(pdf_text.ocr_schema(proc)["required"], ["raw_text"])


class FileVersionsTest(unittest.TestCase):
    """``file_versions``: предыдущая версия файла по diff вложений двух версий XML."""

    @staticmethod
    def body(*files, print_url=None):
        """Тело XML с вложениями ``(имя, ссылка)`` и печатной формой."""
        b = {"attachmentsInfo": {"attachmentInfo": [{"fileName": n, "docDescription": n, "url": u} for n, u in files]}}
        if print_url:
            b["printFormInfo"] = {"url": print_url}
        return b

    def test_replaced(self):
        """Заменённое вложение → файл со старой ссылкой."""
        from risk_monitoring import file_versions as fv

        old = self.body(("Проект контракта.docx", "https://zakupki.gov.ru/file?uid=A"), ("ТЗ.docx", "https://zakupki.gov.ru/file?uid=T"))
        new = self.body(("Проект контракта.docx", "https://zakupki.gov.ru/file?uid=B"), ("ТЗ.docx", "https://zakupki.gov.ru/file?uid=T"))
        cur = {"id": 20, "file_name": "Проект контракта.docx", "source_url": "http://www.zakupki.gov.ru/file?uid=B"}
        ref = fv.previous_ref(cur, new, old)
        self.assertEqual(ref["match"], "replaced")
        cands = [{"id": 10, "source_url": "https://zakupki.gov.ru/file?uid=A", "xml_source_id": "5", "eis_version": "1"},
                 {"id": 11, "source_url": "https://zakupki.gov.ru/file?uid=T", "xml_source_id": "5"}]
        self.assertEqual(fv.pick_previous(ref, cur, cands, 5)["id"], 10)
        self.assertEqual(fv.previous_ref({"id": 21, "file_name": "ТЗ.docx", "source_url": "https://zakupki.gov.ru/file?uid=T"},
                                         new, old)["match"], "unchanged")

    def test_added_and_print_form(self):
        """Новое вложение — без предыдущей версии; печатная форма — печатная форма прошлого XML."""
        from risk_monitoring import file_versions as fv

        old = self.body(("ТЗ.docx", "u:T"), print_url="https://zakupki.gov.ru/epz/order/notice/printForm/view.html?regNumber=1&v=1")
        new = self.body(("ТЗ.docx", "u:T"), ("Обоснование НМЦК.xlsx", "u:N"),
                        print_url="https://zakupki.gov.ru/epz/order/notice/printForm/view.html?regNumber=1&v=2")
        ref = fv.previous_ref({"id": 1, "file_name": "Обоснование НМЦК.xlsx", "source_url": "u:N"}, new, old)
        self.assertEqual(ref["match"], "added")
        self.assertIsNone(fv.pick_previous(ref, {"id": 1}, [{"id": 2, "source_url": "u:N"}], 5))
        pf = {"id": 3, "file_name": "print_form_notice.html", "source_url": None}
        ref = fv.previous_ref(pf, new, old)
        self.assertEqual(ref["match"], "print_form")
        prev = [{"id": 4, "file_name": "Печатная форма извещения (версия 1).html", "source_url": None, "xml_source_id": 5}]
        self.assertEqual(fv.pick_previous(ref, pf, prev, 5)["id"], 4)

    def test_choose_xml_by_owner(self):
        """XML для файла без xml_source_id: та же версия и группа владельца."""
        from risk_monitoring import file_versions as fv

        cands = [{"id": 1, "tag": "epNotificationEF2020", "eis_version": 1},
                 {"id": 2, "tag": "epNotificationEF2020", "eis_version": 2},
                 {"id": 3, "tag": "contract", "eis_version": 2},
                 {"id": 4, "tag": "epNotificationEF2020", "eis_version": 3}]
        order = [c["id"] for c in fv.choose_xml({"owner_type": "notice", "eis_version": 2}, cands)]
        self.assertEqual(order, [2, 1])
        self.assertTrue(fv.contains(self.body(("a", "https://x/f?uid=1")), {"source_url": "https://x/f?uid=1"}))


class FetchPreviousTest(unittest.TestCase):
    """``AIFileAnalyzer.fetch_previous_versions``: XML по owner_number, diff вложений, запасной путь по имени."""

    def test_flow(self):
        """Файл 20 без xml_source_id находит XML 2 по номеру, затем прошлый XML 1 и заменённое вложение."""
        try:
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")

        body = FileVersionsTest.body
        bodies = {1: body(("Проект контракта.docx", "u:A")), 2: body(("Проект контракта.docx", "u:B"))}
        data = {
            "xml_by_owner.sql": [{"id": 2, "tag": "epNotificationEF2020", "eis_version": 1, "purchase_number": "0373",
                                  "archive_url": "a", "entry_name": "e2"}],
            "xml_previous.sql": [{"current_id": 2, "id": 1, "eis_version": 0, "archive_url": "a", "entry_name": "e1"}],
            "files_by_ref.sql": [{"id": 10, "source_url": "u:A", "xml_source_id": 1, "eis_version": 0, "path": "/f/10"}],
            "files_previous.sql": [{"current_id": 30, "id": 31, "eis_version": 0, "path": None, "source_url": "u:Z"}],
        }
        an = AIFileAnalyzer.__new__(AIFileAnalyzer)
        an.storage_path, an.parser = "https://s", None

        async def query(name, head=(), **lists):
            """Имитация запросов к внешней БД."""
            return [dict(r) for r in data.get(name, [])]

        async def read_xml(archives, x):
            """Имитация чтения XML из архива."""
            return bodies[int(x["id"])]

        an._query, an._read_xml = query, read_xml
        rows = [{"id": 20, "owner_number": "0373", "owner_type": "notice", "eis_version": 1,
                 "file_name": "Проект контракта.docx", "source_url": "u:B"},
                {"id": 30, "owner_number": "9999", "owner_type": "notice", "eis_version": 1, "file_name": "x.pdf"},
                {"id": 40, "owner_number": "0373", "eis_version": 0, "file_name": "y.pdf"}]
        res = asyncio.run(an.fetch_previous_versions(rows))
        self.assertEqual(res[20]["id"], 10)
        self.assertEqual(res[20]["match"], "replaced")
        self.assertEqual(res[20]["url"], "https://s/f/10")
        self.assertEqual(res[30]["match"], "file_name")
        self.assertNotIn(40, res)


class RenamedAttachmentTest(unittest.TestCase):
    """Новая редакция вложения со сменой ссылки и имени (пример извещения 12.11.2024)."""

    def test_renamed_is_replaced(self):
        """«3.Требование к содер., составу заявки….docx» → «3. Требования к содержанию… .doc» — замена."""
        from risk_monitoring import file_versions as fv
        from risk_monitoring import xml_events

        u = "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid="
        def att(name, uid):
            """Вложение XML ЕИС."""
            return {"fileName": name, "docDescription": name.rsplit(".", 1)[0], "url": u + uid}
        old = {"attachmentsInfo": {"attachmentInfo": [
            att("1. Обоснование НМЦК.xlsx", "N0"), att("2. Описание объекта закупки.docx", "44D1"),
            att("Проект контракта.docx", "8890"), att("3.Требование к содер., составу заявки на участие в закупке.docx", "E61E")]}}
        new = {"attachmentsInfo": {"attachmentInfo": [
            att("1. Обоснование НМЦК.xlsx", "N0"), att("2. Описание объекта закупки.docx", "1DD7"),
            att("Проект контракта.docx", "84FD"),
            att("3. Требования к содержанию, составу заявки и инструкция по ее заполнению.doc", "10A6")]}}
        ch = xml_events.document_changes(xml_events.attachments(old), xml_events.attachments(new))
        self.assertEqual((len(ch["added"]), len(ch["removed"]), len(ch["replaced"])), (0, 0, 3))
        renamed = next(r for r in ch["replaced"] if r["after"]["url"].endswith("10A6"))
        self.assertEqual(renamed["before"]["url"], u + "E61E")
        self.assertEqual(renamed["matched_by"], "similar")

        cur = {"id": 7, "file_name": "3. Требования к содержанию, составу заявки и инструкция по ее заполнению.doc",
               "source_url": u + "10A6"}
        ref = fv.previous_ref(cur, new, old)
        self.assertEqual((ref["match"], ref["urls"]), ("replaced", [u + "E61E"]))

    def test_different_documents_not_paired(self):
        """Удалили ТЗ и добавили проект контракта; «Приложение 1» → «Приложение 2» — это не замены."""
        from risk_monitoring import xml_events

        old = [{"url": "a", "file_name": "Техническое задание.docx", "index": 0}]
        new = [{"url": "b", "file_name": "Проект контракта.docx", "index": 0}]
        ch = xml_events.document_changes(old, new)
        self.assertEqual((len(ch["added"]), len(ch["removed"]), len(ch["replaced"])), (1, 1, 0))
        ch = xml_events.document_changes([{"url": "a", "file_name": "1. Приложение к извещению.docx", "index": 3}],
                                         [{"url": "b", "file_name": "2. Приложение к извещению.docx", "index": 3}])
        self.assertEqual(len(ch["replaced"]), 0)


class LLMTimeoutTest(unittest.TestCase):
    """``call_llm``: 504 шлюза — паузы, предохранитель, без повторов SDK; поток собирается из чанков."""

    def setUp(self):
        """Сбрасывает состояние предохранителя и подменяет ``asyncio.sleep`` (паузы — без ожидания)."""
        from risk_monitoring import llm_json

        self.llm_json = llm_json
        llm_json._breaker.clear()
        llm_json._gates.clear()
        self.sleeps = []
        self._sleep = llm_json.asyncio.sleep

        async def fake_sleep(sec):
            """Запоминает паузу вместо ожидания."""
            self.sleeps.append(sec)

        llm_json.asyncio.sleep = fake_sleep

    def tearDown(self):
        """Возвращает ``asyncio.sleep``."""
        self.llm_json.asyncio.sleep = self._sleep

    @staticmethod
    def client(script):
        """Клиент: ``script`` — список ответов (строка — текст потоком, int — HTTP-ошибка)."""
        class Err(Exception):
            """HTTP-ошибка сервера."""
            def __init__(self, code):
                super().__init__(f"HTTP {code}")
                self.status_code = code

        class Stream:
            """Поток чанков ответа."""
            def __init__(self, text):
                self.chunks = [types.SimpleNamespace(choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=t))])
                               for t in (text[:3], text[3:])]

            def __aiter__(self):
                return self._gen()

            async def _gen(self):
                for c in self.chunks:
                    yield c

        calls = []

        async def create(**kw):
            """Очередной ответ сценария."""
            calls.append(kw)
            item = script.pop(0)
            if isinstance(item, int):
                raise Err(item)
            return Stream(item)

        c = types.SimpleNamespace(base_url="http://llm", chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        c.with_options = lambda **kw: (calls.append({"options": kw}), c)[1]
        return c, calls

    def test_retry_after_504(self):
        """Два 504 → пауза 5 и 15 с (± разброс) → ответ потоком; SDK-повторы отключены."""
        c, calls = self.client([504, 504, '{"ok": 1}'])
        text = asyncio.run(self.llm_json.call_llm(c, model="m", messages=[]))
        self.assertEqual(text, '{"ok": 1}')
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(4 <= self.sleeps[0] <= 6.5 and 12 <= self.sleeps[1] <= 19.5)
        self.assertIn({"options": {"max_retries": 0}}, calls)
        self.assertTrue(all(k.get("stream") for k in calls if "model" in k))

    def test_breaker_and_permanent_error(self):
        """Три сбоя подряд открывают паузу 30 с; 400 не повторяется."""
        c, _ = self.client([504, 504, 504, "ok"])
        self.assertEqual(asyncio.run(self.llm_json.call_llm(c, model="m", messages=[])), "ok")
        self.assertTrue(any(25 <= s <= 30 for s in self.sleeps))
        c, calls = self.client([400, "ok"])
        with self.assertRaises(Exception):
            asyncio.run(self.llm_json.call_llm(c, model="m", messages=[]))
        self.assertEqual(len([k for k in calls if "model" in k]), 1)


class ResilientClientTest(LLMTimeoutTest):
    """``ResilientClient``: обёртка общего клиента — пауза вместо повторов SDK, поток без вложенных повторов."""

    def test_proxy_retry_and_passthrough(self):
        """504 → пауза ~5 с → ответ; повторы SDK отключены; call_llm не повторяет поверх обёртки."""
        from configs.llm_resilience import ResilientClient, call_llm

        raw, calls = self.client([504, "ok"])

        async def plain(**kw):
            """Обычный (не потоковый) ответ поверх сценария."""
            r = await raw_create(**kw)
            text = "".join(c.choices[0].delta.content for c in r.chunks)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))])

        raw_create = raw.chat.completions.create
        raw.chat.completions.create = plain
        rc = ResilientClient(raw)
        resp = asyncio.run(rc.chat.completions.create(model="m", messages=[]))
        self.assertEqual(resp.choices[0].message.content, "ok")
        self.assertEqual(len(self.sleeps), 1)
        self.assertIn({"options": {"max_retries": 0}}, calls)
        self.assertIs(rc.base_url, raw.base_url)

        raw2, calls2 = self.client([504, 504, "ok"])
        rc2 = ResilientClient(raw2)
        self.assertEqual(asyncio.run(call_llm(rc2, model="m", messages=[])), "ok")
        self.assertEqual(len([k for k in calls2 if "model" in k]), 3)   # 1 + 2 повтора call_llm, без умножения


if __name__ == "__main__":
    unittest.main()


class Purchase1405Test(unittest.TestCase):
    """Закупка 0354100008624000011 (risk_monitoring_contract_id 1405): вложения прошлой редакции извещения
    (3203–3205) загружены с eis_version = 2, как и новые (3196–3198); xml_source_id не заполнен."""

    U = "https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid="

    def setUp(self):
        """XML версий 1 и 2 и анализатор с подменёнными запросами."""
        try:
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")

        def att(name, uid):
            """Вложение XML."""
            return {"fileName": name, "docDescription": name.rsplit(".", 1)[0], "url": self.U + uid}

        common = [att("Сводный сметный расчет.xlsx", "C4F9")]
        v1 = {"attachmentsInfo": {"attachmentInfo": common + [
            att("5.ПРОЕКТ ГПД.docx", "44D1"), att("1.Описание объекта закупки.docx", "8890"),
            att("3.Требование к содер., составу заявки на участие в закупке.docx", "E61E")]}}
        v2 = {"attachmentsInfo": {"attachmentInfo": common + [
            att("5.ПРОЕКТ ГПД.docx", "1DD7"), att("1.Описание объекта закупки.docx", "84FD"),
            att("3. Требования к содержанию, составу заявки и инструкция по ее заполнению.doc", "10A6")]}}
        self.bodies = {3208: v1, 3209: v2}
        xmls = [{"id": 3208, "entry_name": "epNotificationEF2020_0354100008624000011_1_0192.xml", "eis_version": 1,
                 "tag": "epNotificationEF2020", "purchase_number": "0354100008624000011", "archive_url": "a"},
                {"id": 3209, "entry_name": "epNotificationEF2020_0354100008624000011_2_0193.xml", "eis_version": 2,
                 "tag": "epNotificationEF2020", "purchase_number": "0354100008624000011", "archive_url": "a"},
                {"id": 3210, "entry_name": "epProtocolEF2020SubmitOffers_0354100008624000011_1_0193.xml",
                 "eis_version": 1, "tag": "epProtocolEF2020SubmitOffers", "purchase_number": "0354100008624000011"}]
        files = [{"id": 3198, "source_url": self.U + "10A6", "xml_source_id": None, "eis_version": 2,
                  "file_name": "3. Требования к содержанию, составу заявки и инструкция по ее заполнению.doc", "path": "/p/3198"},
                 {"id": 3205, "source_url": self.U + "E61E", "xml_source_id": None, "eis_version": 2,
                  "file_name": "3.Требование к содер., составу заявки на участие в закупке.docx", "path": "/p/3205"}]

        async def query(name, head=(), **lists):
            """Запросы к внешней БД по данным CSV."""
            if name == "xml_by_owner.sql":
                return [dict(x) for x in xmls]
            if name == "xml_previous.sql":
                return [{"current_id": 3209, **xmls[0]}] if 3209 in lists["ids"] else []
            if name == "files_by_ref.sql":
                return [dict(f) for f in files if f["source_url"] in lists["urls"]]
            return []

        async def read_xml(archives, x):
            """XML из «архива»."""
            return self.bodies[int(x["id"])]

        an = AIFileAnalyzer.__new__(AIFileAnalyzer)
        an.storage_path, an.parser = "https://s", None
        an._query, an._read_xml = query, read_xml
        self.an = an
        self.rows = [{"id": f["id"], "owner_number": "0354100008624000011", "owner_type": "notice",
                      "eis_version": 2, "xml_source_id": None, "file_name": f["file_name"], "source_url": f["source_url"]}
                     for f in files]

    def test_old_attachment_superseded_new_finds_previous(self):
        """3205 — прошлая редакция: XML версии 1, предыдущего нет, заменён файлом 3198; у 3198 предыдущая — 3205."""
        res = asyncio.run(self.an.fetch_previous_versions(self.rows))
        self.assertEqual(res[3198]["id"], 3205)
        self.assertEqual(res[3198]["match"], "replaced")
        self.assertNotIn(3205, res)
        lk = self.an.lookups[3205]
        self.assertEqual((lk["xml_id"], lk["xml_eis_version"], lk["match"]), (3208, 1, "no_previous_xml"))
        self.assertEqual(lk["superseded_by"]["file_id"], 3198)
        self.assertIn("eis_version = 2", lk["note"])


class RiskDedupeTest(unittest.TestCase):
    """Один типовой пункт под четырьмя кодами (файл 3205) — не больше двух, без синонимов."""

    def test_merge(self):
        """DOC-005/AI-005 и DOC-006/AI-003 на одной цитате → по одному коду из группы."""
        from risk_monitoring.document_risk_analyzer import DocumentRiskAnalyzer

        an = DocumentRiskAnalyzer.__new__(DocumentRiskAnalyzer)
        frag = "10. Участником закупки в заявке устанавливаются характеристики и показатели товара"
        risks = [{"code": c, "severity": "medium", "evidence": {"fragment": frag, "confidence": 0.9}}
                 for c in ("DOC-005", "DOC-006", "AI-003", "AI-005")]
        risks.append({"code": "DOC-004", "severity": "high", "evidence": {"fragment": "иной пункт", "confidence": 0.95}})
        codes = sorted(r["code"] for r in an._merge(risks))
        self.assertEqual(len([c for c in codes if c != "DOC-004"]), 2)
        self.assertIn("DOC-004", codes)
        self.assertFalse({"DOC-006", "AI-003"} <= set(codes))
        self.assertFalse({"DOC-005", "AI-005"} <= set(codes))


class PrintTitleTest(unittest.TestCase):
    """Заголовок печатной формы, перенесённый на две строки."""

    def test_title(self):
        """«Изменения извещения о проведении» + «запроса котировок … №…» — одна строка."""
        from knowledge_store.eis_printform import PrintForm
        from risk_monitoring.printform_passport import _title

        form = PrintForm("Печатная форма\nИзменения извещения о проведении\nзапроса котировок в электронной форме "
                         "для закупки №0372100039726000019\nОбщая информация\nНомер извещения\n0372100039726000019")
        self.assertEqual(_title(form), "Изменения извещения о проведении запроса котировок в электронной форме "
                                       "для закупки №0372100039726000019")


class StatutoryFilterTest(unittest.TestCase):
    """Требования, установленные законом, и типовые формулировки инструкции — не риск (файл 3205)."""

    def test_statutory(self):
        """19.28 КоАП, безотзывная гарантия, «или»/«либо» — отбрасываются; опыт сверх закона — нет."""
        from risk_monitoring.document_risk_analyzer import statutory

        drop = [("DOC-004", "не было привлечено к административной ответственности за совершение "
                            "административного правонарушения, предусмотренного статьей 19.28"),
                ("DOC-005", "Независимая гарантия должна быть безотзывной и должна содержать: 1) сумму"),
                ("DOC-006", "«или», «либо» - участником предлагается одно или несколько характеристик товара"),
                ("PROC-012", "«или», «либо» - участником предлагается одно или несколько характеристик")]
        for code, frag in drop:
            self.assertTrue(statutory({"code": code, "evidence": {"fragment": frag}}), code)
        keep = {"code": "DOC-004", "evidence": {"fragment": "Опыт реставрации объектов в Москве не менее 5 лет"}}
        self.assertFalse(statutory(keep))
        self.assertFalse(statutory({"code": "DOC-002", "evidence": {"fragment": "статьей 19.28"}}))


class SupersededSkipTest(unittest.TestCase):
    """Файл прошлой редакции с известной заменой — паспорт без скачивания и без модели."""

    def test_skip(self):
        """3205 заменён файлом 3198 — pipeline.result = superseded; в режиме отладки — обычный анализ."""
        import sys
        from unittest import mock

        tde = types.ModuleType("evaluate_documents.type_data_extractor")
        tde.DOCUMENT_TYPE_MAPPING = {"docTrebContentRequestFiles": "Требования к содержанию заявки"}
        with mock.patch.dict(sys.modules, {"evaluate_documents.type_data_extractor": tde}):
            try:
                from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
            except ImportError as e:
                raise unittest.SkipTest(f"нет зависимостей: {e}")
            an = AIFileAnalyzer.__new__(AIFileAnalyzer)
            an.force, an.model, an.versions = False, "m", {}
            an.lookups = {3205: {"superseded_by": {"file_id": 3198, "xml_eis_version": 2, "file_name": "3. Треб.doc"}}}
            row = {"id": 3205, "file_name": "3.Требование к содер., составу заявки на участие в закупке.docx"}
            ai = an._superseded_analysis(row, None, 1)
            self.assertEqual(ai["pipeline"]["result"], "superseded")
            self.assertEqual(ai["superseded_by"]["file_id"], 3198)
            self.assertEqual(ai["doc_type"]["detected_type"], "docTrebContentRequestFiles")
            an.force = True
            self.assertIsNone(an._superseded_analysis(row, None, 1))


class Run1015Test(unittest.TestCase):
    """Прогон 15.10.2026: 3198 / 3205 (.doc и .docx), итоги таблицы НМЦК 1097, требования ПП № 2571 и СРО."""

    def test_doc_docx_unwrap(self):
        """Жёсткие переносы .doc не мешают сравнению с .docx той же редакции."""
        from risk_monitoring.version_diff import text_diff

        doc = ("1. Подать заявку на участие в закупке вправе только\nзарегистрированный участник закупки.\n"
               "2. Участник закупки вправе подать\nтолько одну заявку.")
        docx = ("1. Подать заявку на участие в закупке вправе только зарегистрированный участник закупки.\n"
                "2. Участник закупки вправе подать только одну заявку.")
        self.assertEqual(text_diff(doc, docx)["similarity"], 1.0)

    def test_filters(self):
        """Строка «ИТОГО» с несколькими суммами, СРО и опыт по ПП № 2571 — не риски."""
        from risk_monitoring.document_risk_analyzer import statutory, table_total

        self.assertTrue(table_total({"code": "FIN-015", "evidence": {"fragment": "ИТОГО 1 046 200,00 1 033 410,00 1 026 336,64"}}))
        self.assertFalse(table_total({"code": "DOC-002", "evidence": {"fragment": "ИТОГО 1 046 200,00 1 033 410,00"}}))
        self.assertTrue(statutory({"code": "DOC-004", "evidence": {"fragment": "Участник закупки должен быть членом СРО"}}))
        self.assertTrue(statutory({"code": "DOC-005", "evidence": {
            "fragment": "не менее 50 процентов начальной (максимальной) цены контракта, заключаемого по результатам"}}))

    def test_previous_codes(self):
        """Прошлая редакция без рисков (rule_ids NULL, статус completed) — пустой список, а не «нет данных»."""
        try:
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        an = AIFileAnalyzer.__new__(AIFileAnalyzer)
        an.versions, an.model = {"risk_analysis": "r"}, "m"

        async def no_cache(*a):
            return None

        an._get_cached = no_cache
        self.assertEqual(asyncio.run(an._previous_codes({"rule_ids": None, "rm_status": "completed"}, None, [])), [])
        self.assertEqual(asyncio.run(an._previous_codes({"rule_ids": '["DOC-004"]'}, None, [])), ["DOC-004"])
        self.assertIsNone(asyncio.run(an._previous_codes({"rule_ids": None}, {"doc_id": None}, [])))


class Run1015bTest(unittest.TestCase):
    """Вторая проверка 15.10.2026: порядок редакций, схожесть по словам, источник требования, файлы XML."""

    def test_waves(self):
        """3205 (прошлая) раньше 3198 (новой); независимый файл — в первой волне."""
        try:
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        waves = AIFileAnalyzer._waves([3198, 3205, 1096], {3198: {"id": 3205}}, ["3198", "3205", "1096"])
        self.assertEqual(waves, [[3205, 1096], [3198]])
        # прошлая редакция уже проанализирована (не в todo) — ждать нечего
        self.assertEqual(AIFileAnalyzer._waves([3198, 3205], {3198: {"id": 3205}}, [3198]), [[3198, 3205]])

    def test_word_similarity(self):
        """Таблица и текст с тем же содержанием: по абзацам 0, по словам — высокая."""
        from risk_monitoring.version_diff import text_diff

        old = "Требования к участнику | Участник должен быть зарегистрирован на электронной площадке и подать одну заявку"
        new = "Требования к участнику:\nУчастник должен быть зарегистрирован на электронной площадке и подать одну заявку."
        d = text_diff(old, new)
        self.assertEqual(d["similarity_paragraphs"], 0.0)
        self.assertGreater(d["similarity_words"], 0.7)
        self.assertEqual(d["similarity"], d["similarity_words"])

    def test_requirement_source_schema(self):
        """В схеме ответа риска обязательно поле requirement_source."""
        import json as _json

        schema = _json.load(open("schemas/risk_analysis_schema.json", encoding="utf-8"))
        item = schema["properties"]["risks"]["items"]
        self.assertIn("requirement_source", item["required"])
        self.assertEqual(item["properties"]["requirement_source"]["enum"], ["закон", "заказчик", "неясно"])

    def test_pending_files(self):
        """Файлы XML: по вложениям, печатной форме и xml_source_id; проанализированные — пропускаются."""
        from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

        xa = XmlEventAnalyzer.__new__(XmlEventAnalyzer)
        xa.storage_path = "s"
        body = (b'<epNotificationEF2020><attachmentsInfo><attachmentInfo><fileName>a.docx</fileName>'
                b'<url>https://z/f?uid=A</url></attachmentInfo></attachmentsInfo>'
                b'<printFormInfo><url>https://z/print?id=1</url></printFormInfo></epNotificationEF2020>')
        seen = {}

        async def query(name, head=(), **lists):
            """Внешняя БД."""
            if name == "xml_meta.sql":
                return [{"id": 3209, "archive_url": "a", "entry_name": "e"}]
            seen.update(lists)
            return [{"id": 3198, "rm_status": None}, {"id": 3199, "rm_status": "completed"}]

        class Arch:
            async def read(self, url, entry):
                return body

        xa._query, xa.archives = query, Arch()
        try:
            ids = asyncio.run(xa.pending_files([3209]))
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        self.assertEqual(ids, [3198])
        self.assertIn("https://z/f?uid=A", seen["urls"])
        self.assertIn("https://z/print?id=1", seen["urls"])
        self.assertEqual(asyncio.run(xa.pending_files([3209], include_done=True)), [3198, 3199])


class ModelRestartTest(unittest.TestCase):
    """Перезапуск модели 15.10.2026 19:02–19:06: 500 → 503 → 404 «Model not found»."""

    def test_rm_client_retries(self):
        """Клиент извлечения риск-мониторинга: 6 попыток вместо 2, вложенная обёртка не дублируется."""
        from configs.llm_resilience import ResilientClient

        raw = types.SimpleNamespace(chat=None)
        rc = ResilientClient(ResilientClient(raw), retries=6)
        self.assertIs(rc.raw_client, raw)
        self.assertEqual(rc.chat.completions._retries, 6)

    def test_ocr_error_marker(self):
        """Текст с ошибкой OCR страницы распознаётся как неполный."""
        try:
            from risk_monitoring.ai_file_analyzer import OCR_ERROR_RE
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        self.assertTrue(OCR_ERROR_RE.search('Страница 2 (OCR): {"has_text": false, "processing_quality": {"status": "error"}}'))
        self.assertTrue(OCR_ERROR_RE.search("Страница 3 (OCR):\n[Ошибка при анализе изображения]"))
        self.assertFalse(OCR_ERROR_RE.search("Страница 1:\nСтатус заявки: ошибка заполнения"))

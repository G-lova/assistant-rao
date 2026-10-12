"""Тесты контроля качества анализа файлов риск-мониторинга (по итогам прогона файлов 1096/1097):
фильтр OCR, вид документа и печатные формы, допустимые коды, очистка паспорта, фильтры рисков,
переход на схему в промпте, если сервер не поддерживает guided_json.
"""
import asyncio
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


if __name__ == "__main__":
    unittest.main()

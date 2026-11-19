"""Извлечение данных файлов РМ (19.11.2026): XML и платёжки — кодом, профиль полей по типу."""
import asyncio
import unittest
from unittest import mock

from configs import settings
from risk_monitoring import code_extraction as ce

# XML контракта ЕИС, прочитанный FileReader.read_xml_file («тег: значение»)
EIS_XML = """regNum: 1772401234525000123
number: 0373100029526000099
signDate: 2026-11-10
contractSubject: Поставка медицинских изделий для нужд учреждения
price: 1250000.50
startDate: 2026-11-10
endDate: 2026-12-31
fullName: ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ ЗДРАВООХРАНЕНИЯ «БОЛЬНИЦА № 1»
INN: 7707083893
KPP: 770701001
fullName: ООО «Поставщик»
INN: 7736050003
KPP: 773601001
name: Шприц одноразовый
price: 12.30
number: 2
"""

PAYMENT = """ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 123
10.11.2026
Сумма 2076618-96
ИНН 7707083893
ГБУЗ Больница
ИНН 7736050003
ООО Поставщик
Оплата по контракту № 0373100029526000099 от 10.11.2026
"""


class ParseXmlTest(unittest.TestCase):
    """Реквизиты XML ЕИС кодом: первое вхождение — шапка документа."""

    def test_eis_contract(self):
        """Номер, реестровый номер, цена (не цена позиции), даты, срок, предмет, стороны с КПП."""
        raw = ce.parse_xml(EIS_XML)
        self.assertEqual(raw["contract_number"]["value"], "0373100029526000099")
        prices = [f for f in raw["finances"] if f["kind"] == "contract_price"]
        self.assertEqual([p["value"] for p in prices], [1250000.5])
        self.assertEqual({d["kind"]: d["value"] for d in raw["dates"]},
                         {"signing": "2026-11-10", "contract_start": "2026-11-10", "contract_end": "2026-12-31"})
        self.assertEqual(raw["planned_timeline"]["end_date"], "2026-12-31")
        self.assertIn("медицинских", raw["procurement_subject"]["description"])
        self.assertEqual([(o["inn"], o["kpp"]) for o in raw["organizations"]],
                         [("7707083893", "770701001"), ("7736050003", "773601001")])
        self.assertTrue(raw["organizations"][1]["name"].startswith("ООО"))
        self.assertTrue(any("Реестровый номер" in o["value"] for o in raw["other_data"]))

    def test_not_xml(self):
        """Обычный текст — не XML."""
        self.assertIsNone(ce.parse_xml("Контракт № 1\nНастоящий контракт заключён …\nСтороны договорились …"))
        self.assertFalse(ce.is_flat_xml("Контракт.pdf", EIS_XML))
        self.assertTrue(ce.is_flat_xml("Электронный контракт.xml", EIS_XML))

    def test_number_without_regnum(self):
        """Без реестрового номера общий тег number не принимается за номер контракта."""
        raw = ce.parse_xml("\n".join(["number: 15", "name: Позиция", "quantity: 3", "price: 100", "unit: шт"]))
        self.assertIsNone(raw)

    def test_merge_with_model(self):
        """Код поверх ответа модели: номер и цена из XML, описание модели сохраняется; без модели — шаблон."""
        model = {"readability": {"status": "allow"}, "raw_data": {"summary": "Контракт на поставку.", "finances": []}}
        out = ce.xml_extraction(model, ce.parse_xml(EIS_XML), "Электронный контракт.xml")
        self.assertEqual(out["source"], "xml_code")
        self.assertEqual(out["raw_data"]["summary"], "Контракт на поставку.")
        self.assertEqual(out["raw_data"]["contract_number"]["value"], "0373100029526000099")
        alone = ce.xml_extraction(None, ce.parse_xml(EIS_XML), "Электронный контракт.xml")
        self.assertIn("№ 0373100029526000099", alone["raw_data"]["summary"])
        self.assertEqual(alone["readability"]["status"], "allow")


class PaymentTest(unittest.TestCase):
    """Платёжное поручение — без модели."""

    def test_payment(self):
        """Номер, дата, сумма разобраны — результат кодом."""
        out = ce.payment_extraction(PAYMENT, "пп 123.pdf")
        self.assertEqual(out["source"], "payment_code")
        self.assertTrue(any(f["value"] == 2076618.96 for f in out["raw_data"]["finances"]))
        self.assertIsNone(ce.payment_extraction("Акт сдачи-приёмки работ", "акт.pdf"))


class ExtractTest(unittest.TestCase):
    """AIFileAnalyzer._extract: профиль, XML — только первые блоки модели, платёжка — без модели, кэш."""

    def _fa(self):
        """Анализатор с подменой модели и кэша."""
        from tests.stubs import stubbed_import

        ctx = stubbed_import("risk_monitoring.ai_file_analyzer")
        m = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        fa = m.AIFileAnalyzer.__new__(m.AIFileAnalyzer)
        fa.versions = {"data_extraction": "v1"}
        fa.recompute = False
        fa.extraction_model = "m"
        calls, saved, cache = [], [], {}

        class Extractor:
            """Модель извлечения: запоминает блоки и профиль."""

            async def extract_data_from_document(self, chunks, filename, profile=None, hint="", **kw):
                """Ответ модели."""
                calls.append({"chunks": list(chunks), "profile": profile, "hint": hint})
                return {"readability": {"status": "allow"}, "raw_data": {"summary": "из модели"}}

        class Repo:
            """Кэш ai_document_analysis в памяти."""

            async def get_analysis(self, doc, kind, version, model):
                """Чтение."""
                return cache.get((doc, kind, version))

            async def save_analysis(self, doc, kind, version, model, result, compact, conf):
                """Запись."""
                saved.append(version)
                cache[(doc, kind, version)] = {"result": result}

        fa.extractor, fa.repo = Extractor(), Repo()
        return fa, calls, saved, cache

    def _run(self, fa, *a, **kw):
        """Вызов _extract с включёнными профилями и кодом."""
        with mock.patch.object(settings, "RM_EXTRACTION_PROFILES", True), \
                mock.patch.object(settings, "RM_CODE_EXTRACTION", True), \
                mock.patch.object(settings, "RM_XML_MODEL_CHUNKS", 1):
            return asyncio.run(fa._extract(*a, **kw))

    def test_profile(self):
        """Допсоглашение: профиль supplement, подсказка модели, версия кэша с профилем."""
        fa, calls, saved, _ = self._fa()
        res, meta = self._run(fa, 1, ["текст"], "текст", "ДС 2.docx", 1, None, "docDopConsentContractFiles",
                              "Дополнительное соглашение")
        self.assertEqual(calls[0]["profile"], "supplement")
        self.assertIn("Заполняй только поля", calls[0]["hint"])
        self.assertEqual(saved, ["v1:p=supplement"])
        self.assertEqual((meta["source"], meta["profile"]), ("model", "supplement"))

    def test_full_schema_cache_reused(self):
        """Результат полной схемы в кэше годится для профиля — без запроса к модели."""
        fa, calls, saved, cache = self._fa()
        cache[(1, "data_extraction", "v1")] = {"result": {"raw_data": {"summary": "полная схема"}}}
        res, _ = self._run(fa, 1, ["текст"], "текст", "ДС.docx", 1, None, "docDopConsentContractFiles", "ДС")
        self.assertEqual(res["raw_data"]["summary"], "полная схема")
        self.assertEqual(calls, [])

    def test_xml_first_chunk_only(self):
        """XML: модели — первый блок из трёх, реквизиты — кодом."""
        fa, calls, saved, _ = self._fa()
        res, meta = self._run(fa, 2, [EIS_XML, "b2", "b3"], EIS_XML, "Электронный контракт.xml", 1, None,
                              "docContractPostTovarFiles", "Контракт")
        self.assertEqual(calls[0]["chunks"], [EIS_XML])
        self.assertEqual((meta["source"], meta["model_chunks"], meta["chunks"]), ("xml_code", 1, 3))
        self.assertEqual(res["raw_data"]["contract_number"]["value"], "0373100029526000099")
        self.assertEqual(saved, ["v1:p=contract:xml1"])

    def test_payment_without_model(self):
        """Платёжка — без модели."""
        fa, calls, _, _ = self._fa()
        res, meta = self._run(fa, 3, [PAYMENT], PAYMENT, "пп 123.pdf", 1, None, "docDopMaterialsFiles", "Прочее")
        self.assertEqual(calls, [])
        self.assertEqual(meta["source"], "payment_code")

    def test_switch_off(self):
        """Выключено — полная схема всех блоков, версия кэша прежняя."""
        fa, calls, saved, _ = self._fa()
        with mock.patch.object(settings, "RM_EXTRACTION_PROFILES", False), \
                mock.patch.object(settings, "RM_CODE_EXTRACTION", False):
            res, meta = asyncio.run(fa._extract(4, [EIS_XML, "b2"], EIS_XML, "Электронный контракт.xml", 1, None))
        self.assertEqual((calls[0]["profile"], len(calls[0]["chunks"])), (None, 2))
        self.assertEqual(saved, ["v1"])


if __name__ == "__main__":
    unittest.main()

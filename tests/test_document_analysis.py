"""Разбор документа в /evaluate-documents: тип → профиль → извлечение, разбор кодом, дубли, журнал.

Формат результата ``TypeDataExtractor.extract_data_from_document`` проверяется отдельно: те же ключи
``type_compliance`` / ``readability`` / ``raw_data`` (все 14 полей), что и до разделения на шаги.
"""
import asyncio
import json
import logging
import unittest
from pathlib import Path
from unittest import mock

from evaluate_documents.doc_profiles import (
    PROFILES, annotate_pages, describe_raw_data, profile_for, profile_hint, schema_for_profile,
)
from evaluate_documents.merge_utils import dedupe_raw_data
from evaluate_documents.rule_extractors import (
    looks_like_flat_xml, overlay, parse_flat_xml, parse_payment_order,
)
from tests.stubs import stubbed_import

PAYMENT_TEXT = """                                                                       0401060
      22.10.2025               22.10.2025
    Поступ. в банк плат.     Списано со сч. плат.
 ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 4137                                    20.10.2025
 Сумма
            Два миллиона семьдесят шесть тысяч шестьсот восемнадцать рублей 96 копеек
 прописью
ИНН 7021000043               КПП 701701001                   Сумма 2076618-96
УФК ПО НОВОСИБИРСКОЙ ОБЛАСТИ (ТУСУР)                         Сч. №    03215643000000015100
Плательщик
ИНН 7017464476                   КПП 701701001                         Сч. №      40702810024400000080
ООО "ГОРИЗОНТ"
Получатель
текущий ремонт помещения по сч 8 от 07.10.2025, Договор от 15.09.2025 № ЗА-8/487/25 НДС0.00
Назначение платежа"""

XML_TEXT = """id: 298255
code: 250001482400144-250001482400144-1-511856-475
customer_code: ЗА-8/487/25
name: Выполнение работ по текущему ремонту помещений
amount: 33728698.15
upload_date: 2025-09-04T10:25:16.462969
sign_date: 2025-09-11 19:49"""

XML_PROTOCOL = """id: 563548
value: Электронный аукцион
external_code: 32515110042
max_price: 33898189.10
sign_date: 2025-09-01
price: 33728698.15
percentage_reduction: 0.50"""


class ProfilesTest(unittest.TestCase):
    """Профили полей по типу документа."""

    def test_profile_for(self):
        """Платёжка — по имени или тексту; тип → профиль; акт/ДС в «доп. материалах» — по имени файла."""
        self.assertEqual(profile_for("docDopMaterialsFiles", "платежка-от-22.10.2025.pdf"), "payment")
        self.assertEqual(profile_for("docValidAllIfFiles", "scan.pdf", "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 1"), "payment")
        self.assertEqual(profile_for("docContractDoWorkFiles", "Договор.docx"), "contract")
        self.assertEqual(profile_for("docDocPriemActSdachFiles", "a.pdf"), "act")
        self.assertEqual(profile_for("docDopMaterialsFiles", "КС-2 от 05.11.2025.pdf"), "act")
        self.assertEqual(profile_for("docDopMaterialsFiles", "ДС-№2.pdf"), "supplement")
        self.assertEqual(profile_for("docDopMaterialsFiles", "Карта партнёра.docx"), "general")
        self.assertEqual(profile_for(None, ""), "general")

    def test_profiles_are_subsets(self):
        """Каждый профиль — подмножество полей схемы, с названием и описанием документа; НМЦК не в договоре и акте."""
        schema = json.loads(Path("schemas/data_extractor_schema.json").read_text(encoding="utf-8"))
        all_fields = set(schema["properties"]["raw_data"]["properties"])
        for name, fields in PROFILES.items():
            self.assertTrue(set(fields) <= all_fields, name)
            self.assertIn("document_name", fields)
            self.assertIn("summary", fields)
        for name in ("contract", "act", "payment", "certificate"):
            self.assertNotIn("nmck_method", PROFILES[name])

    def test_schema_for_profile(self):
        """Схема профиля: только его поля; полная — без изменений; исходная схема не меняется."""
        schema = json.loads(Path("schemas/data_extractor_schema.json").read_text(encoding="utf-8"))
        act = schema_for_profile(schema, "act")
        self.assertEqual(set(act["properties"]["raw_data"]["properties"]), set(PROFILES["act"]))
        self.assertIn("readability", act["properties"])
        self.assertIs(schema_for_profile(schema, None), schema)
        self.assertIn("nmck_method", schema["properties"]["raw_data"]["properties"])
        self.assertIn("акт", profile_hint("act", "docDocPriemActSdachFiles"))
        self.assertEqual(profile_hint("full"), "")

    def test_annotate_pages(self):
        """Блок с середины страницы получает метку продолжения; блок со своей страницей — нет."""
        chunks = ["Страница 1 (OCR): начало\n\nPage 2:\nтекст", "продолжение второй", "Страница 3 (OCR): третья"]
        out = annotate_pages(chunks)
        self.assertEqual(out[0], chunks[0])
        self.assertTrue(out[1].startswith("[Продолжение страницы 2]"))
        self.assertEqual(out[2], chunks[2])
        self.assertEqual(annotate_pages(["без страниц", "и тут"]), ["без страниц", "и тут"])

    def test_describe(self):
        """Краткая сводка для журнала."""
        text = describe_raw_data({"contract_number": {"value": "1"}, "finances": [{}, {}], "dates": []})
        self.assertEqual(text, "номер=1, сумм 2")
        self.assertEqual(describe_raw_data({}), "ничего не извлечено")


class PaymentOrderTest(unittest.TestCase):
    """Платёжное поручение кодом."""

    def test_full(self):
        """Номер, дата, сумма «2076618-96», ИНН сторон, номер договора из назначения."""
        res = parse_payment_order(PAYMENT_TEXT)
        self.assertTrue(res["complete"])
        raw = res["raw_data"]
        self.assertEqual(raw["document_name"]["value"], "Платёжное поручение № 4137")
        self.assertEqual([(f["kind"], f["value"]) for f in raw["finances"]], [("payment", 2076618.96)])
        self.assertEqual([(d["kind"], d["value"]) for d in raw["dates"]], [("payment", "2025-10-20")])
        self.assertEqual(raw["contract_number"]["value"], "ЗА-8/487/25")
        self.assertEqual([(o["role"], o["inn"]) for o in raw["organizations"]],
                         [("плательщик", "7021000043"), ("получатель", "7017464476")])
        self.assertEqual(raw["organizations"][1]["name"], 'ООО "ГОРИЗОНТ"')
        self.assertIn("2 076 618,96", raw["summary"])
        for f in raw["finances"] + raw["dates"]:
            self.assertIn(f["evidence"]["fragment"], PAYMENT_TEXT)

    def test_incomplete_and_other(self):
        """Без суммы — неполный разбор; не платёжка — None."""
        res = parse_payment_order(PAYMENT_TEXT.replace("Сумма 2076618-96", ""))
        self.assertFalse(res["complete"])
        self.assertIsNone(parse_payment_order("Договор подряда № 1"))
        self.assertIsNone(parse_payment_order(""))


class FlatXmlTest(unittest.TestCase):
    """XML площадки («тег: значение»)."""

    def test_contract_card(self):
        """Номер договора и цена из карточки договора."""
        self.assertTrue(looks_like_flat_xml(XML_TEXT))
        raw = parse_flat_xml(XML_TEXT)
        self.assertEqual(raw["contract_number"]["value"], "ЗА-8/487/25")
        self.assertEqual([(f["kind"], f["value"]) for f in raw["finances"]], [("contract_price", 33728698.15)])
        self.assertEqual([d["kind"] for d in raw["dates"]], ["other"])   # sign_date — подпись, не заключение

    def test_protocol(self):
        """НМЦК, ценовое предложение и реестровый номер закупки из протокола."""
        raw = parse_flat_xml(XML_PROTOCOL)
        self.assertEqual(sorted((f["kind"], f["value"]) for f in raw["finances"]),
                         [("nmck", 33898189.1), ("offer_price", 33728698.15)])
        self.assertEqual(raw["other_data"][0]["value"], "Реестровый номер закупки в ЕИС: 32515110042")

    def test_not_xml(self):
        """Обычный текст — не XML."""
        self.assertFalse(looks_like_flat_xml("Договор подряда\nЦена: 100 руб.\nСтороны: А и Б"))
        self.assertIsNone(parse_flat_xml("просто текст"))


class OverlayAndDedupeTest(unittest.TestCase):
    """Наложение значений кода на ответ модели и удаление дублей."""

    def test_overlay(self):
        """Совпавшей сумме модели проставляется вид; новая сумма добавляется; номер — если у модели пусто."""
        raw = {"finances": [{"value": 33728698.15, "context": "Цена"}], "dates": [],
               "contract_number": {"value": None, "evidence": None}, "other_data": [], "organizations": []}
        overlay(raw, parse_flat_xml(XML_PROTOCOL))
        self.assertEqual(raw["finances"][0]["kind"], "offer_price")
        self.assertIn(33898189.1, [f["value"] for f in raw["finances"]])
        overlay(raw, parse_flat_xml(XML_TEXT))
        self.assertEqual(raw["contract_number"]["value"], "ЗА-8/487/25")
        self.assertEqual(len(raw["other_data"]), 1)

    def test_dedupe(self):
        """Одинаковые сумма/вид, дата/вид, сторона с тем же ИНН и ролью — один раз; реквизиты дополняются."""
        raw = {"finances": [{"value": 1.0, "context": "Цена договора"}, {"value": 1.0, "context": "цена  договора."},
                            {"value": 1.0, "kind": "payment"}],
               "dates": [{"value": "2025-01-01", "kind": "act"}, {"value": "2025-01-01T10:00", "kind": "act"},
                         {"value": "2025-01-01", "kind": "signing"}],
               "organizations": [{"name": "ТУСУР", "role": "заказчик", "inn": "7021000043"},
                                 {"name": "ТУСУР (Томск)", "role": "заказчик", "inn": "7021000043", "kpp": "701701001"},
                                 {"name": "ТУСУР", "role": "плательщик", "inn": "7021000043"}]}
        self.assertEqual(dedupe_raw_data(raw), 3)
        self.assertEqual(len(raw["finances"]), 2)
        self.assertEqual(len(raw["dates"]), 2)
        self.assertEqual(len(raw["organizations"]), 2)
        self.assertEqual(raw["organizations"][0]["kpp"], "701701001")


MERGED_KEYS = ["document_name", "summary", "contract_number", "dates", "finances", "nmck_method", "organizations",
               "law_references", "procurement_subject", "items", "planned_timeline", "reporting_documentation",
               "execution_location", "other_data"]


def _analyzer(module, detected="docContractDoWorkFiles", chunk_result=None):
    """TypeDataExtractor с подменёнными шагами: определитель типа и извлечение по блоку."""
    ex = module.TypeDataExtractor.__new__(module.TypeDataExtractor)
    ex.client, ex.model = mock.Mock(), "m"
    ex.semaphore = asyncio.Semaphore(3)
    ex.type_detector = mock.Mock()
    ex.type_detector.detect = mock.AsyncMock(return_value={"detected_type": detected, "source": "model",
                                                           "confidence": 0.9, "issues": []})
    ex.data_extractor = mock.Mock()
    ex.data_extractor.extract_data_from_chunk = mock.AsyncMock(return_value=chunk_result)
    return ex


CONTRACT_CHUNK = {
    "readability": {"status": "allow", "issues": [], "readability_score": 0.9, "main_language": "ru",
                    "language_confidence": 0.9},
    "raw_data": {
        "document_name": {"value": "Договор № ЗА-8/487/25", "confidence": 0.9},
        "summary": "Договор подряда на ремонт.",
        "contract_number": {"evidence": {"fragment": "Договор № ЗА-8/487/25"}, "value": "ЗА-8/487/25"},
        "finances": [{"evidence": {"fragment": "Цена 33 728 698,15"}, "kind": "contract_price", "value": 33728698.15,
                      "context": "Цена договора"},
                     {"evidence": {"fragment": "НМЦК 1 500 000"}, "kind": "nmck", "value": 1500000.0, "context": "НМЦК"}],
        "dates": [{"evidence": {"fragment": "по 01.12.2025"}, "kind": "work_end", "value": "2025-12-01",
                   "context": "Окончание работ"}],
        "organizations": [{"evidence": {"fragment": "ТУСУР"}, "name": "ТУСУР", "role": "заказчик", "inn": None}],
    },
}
CONTRACT_TEXT = "ДОГОВОР № ЗА-8/487/25\nЦена договора 33 728 698,15 руб.\nСрок окончания работ: по 01.12.2025\nЗаказчик ТУСУР"


class AnalyzerTest(unittest.TestCase):
    """Оркестратор TypeDataExtractor: формат прежний, тип — от определителя, извлечение — по профилю."""

    def test_contract_flow_and_format(self):
        """Профиль договора передан извлечению; формат результата прежний; выдуманная НМЦК отброшена."""
        with stubbed_import("evaluate_documents.type_data_extractor") as module:
            ex = _analyzer(module, chunk_result=json.loads(json.dumps(CONTRACT_CHUNK)))
            res = asyncio.run(ex.extract_data_from_document([CONTRACT_TEXT], "Договор.docx",
                                                            "docContractDoWorkFiles", 7))
        call = ex.data_extractor.extract_data_from_chunk.call_args.kwargs
        self.assertEqual(call["profile"], "contract")
        self.assertIn("контракт на выполнение работ", call["hint"].lower())
        self.assertEqual(list(res), ["type_compliance", "readability", "raw_data"])
        self.assertEqual(res["type_compliance"]["status"], "allow")
        self.assertEqual(res["type_compliance"]["detected_type"], "docContractDoWorkFiles")
        for key in MERGED_KEYS:
            self.assertIn(key, res["raw_data"], key)
        self.assertEqual([f["value"] for f in res["raw_data"]["finances"]], [33728698.15])
        self.assertEqual(res["raw_data"]["summary"], "Договор подряда на ремонт.")   # один блок — без запроса summary
        ex.client.chat.completions.create.assert_not_called()

    def test_type_mismatch(self):
        """Определитель нашёл акт в поле договора — deny с прежней формулировкой."""
        with stubbed_import("evaluate_documents.type_data_extractor") as module:
            ex = _analyzer(module, detected="docDocPriemActSdachFiles",
                           chunk_result=json.loads(json.dumps(CONTRACT_CHUNK)))
            res = asyncio.run(ex.extract_data_from_document([CONTRACT_TEXT], "Договор.docx",
                                                            "docContractDoWorkFiles", 7))
        self.assertEqual(res["type_compliance"]["status"], "deny")
        self.assertTrue(res["type_compliance"]["issues"][0].startswith("Ожидался Контракт"))
        self.assertEqual(ex.data_extractor.extract_data_from_chunk.call_args.kwargs["profile"], "act")

    def test_payment_by_code(self):
        """Платёжка разобрана кодом — модель для извлечения не вызывается, формат прежний."""
        with stubbed_import("evaluate_documents.type_data_extractor") as module:
            ex = _analyzer(module, detected="docValidAllIfFiles")
            res = asyncio.run(ex.extract_data_from_document([PAYMENT_TEXT], "платежка-от-22.10.2025.pdf",
                                                            "docDopMaterialsFiles", 7))
        ex.data_extractor.extract_data_from_chunk.assert_not_called()
        raw = res["raw_data"]
        self.assertEqual([(f["kind"], f["value"]) for f in raw["finances"]], [("payment", 2076618.96)])
        self.assertEqual(raw["contract_number"]["value"], "ЗА-8/487/25")
        self.assertEqual(res["readability"]["status"], "allow")
        self.assertEqual(res["type_compliance"]["status"], "allow")
        for key in MERGED_KEYS:
            self.assertIn(key, raw, key)

    def test_xml_overlay(self):
        """XML площадки: значения тегов добавляются к ответу модели."""
        chunk = {"readability": {"status": "allow", "readability_score": 0.9},
                 "raw_data": {"document_name": {"value": "Протокол"}, "summary": "Протокол аукциона.",
                              "finances": []}}
        with stubbed_import("evaluate_documents.type_data_extractor") as module:
            ex = _analyzer(module, detected="docDopMaterialsFiles", chunk_result=chunk)
            res = asyncio.run(ex.extract_data_from_document([XML_PROTOCOL], "document.xml",
                                                            "docDopMaterialsFiles", 7))
        self.assertIn(("nmck", 33898189.1), [(f["kind"], f["value"]) for f in res["raw_data"]["finances"]])

    def test_all_chunks_failed(self):
        """Ни один блок не разобран — прежний ответ deny."""
        with stubbed_import("evaluate_documents.type_data_extractor") as module:
            ex = _analyzer(module, chunk_result=None)
            res = asyncio.run(ex.extract_data_from_document(["текст"], "a.pdf", "docContractDoWorkFiles", 7))
        self.assertEqual(res["type_compliance"]["detected_type"], "unknown")
        self.assertEqual(res["raw_data"], {})


class TypeDetectorTest(unittest.TestCase):
    """Определение типа по началу документа."""

    def test_keywords_fallback(self):
        """Модель не ответила — тип по имени файла, затем по началу текста."""
        with stubbed_import("evaluate_documents.type_detector") as module:
            det = module.TypeDetector.__new__(module.TypeDetector)
            det.detect_document_type = mock.AsyncMock(return_value=None)
            res = asyncio.run(det.detect(["Дополнительное соглашение № 1 к договору"], "scan.pdf"))
            self.assertEqual(res["detected_type"], "docDopConsentContractFiles")
            self.assertEqual(res["source"], "keywords")
            det.detect_document_type = mock.AsyncMock(return_value={"detected_type": "docCertValidFiles",
                                                                    "confidence": 0.8})
            res = asyncio.run(det.detect(["x" * 20000, "y"], "a.pdf"))
            self.assertEqual((res["detected_type"], res["source"]), ("docCertValidFiles", "model"))
            sent = det.detect_document_type.call_args.kwargs["content"]
            self.assertLess(len(sent), 20000)
            self.assertIn("всего блоков текста: 2", sent)


class LogContextTest(unittest.TestCase):
    """Метка экспертизы в журнале."""

    def test_label_in_tasks(self):
        """Метка есть у записей внутри блока и порождённых задач, вне блока — нет."""
        from configs.logger import log_context
        records = []

        class Keep(logging.Handler):
            """Сохраняет сообщения."""

            def emit(self, record):
                """Запоминает готовое сообщение."""
                records.append(record.getMessage())

        log = logging.getLogger("test.log_context")
        log.addHandler(Keep())
        log.setLevel(logging.INFO)
        log.propagate = False

        async def scenario():
            """Две задачи внутри метки и запись снаружи."""
            with log_context("prod:7185"):
                async def child(i):
                    """Запись из задачи."""
                    log.info("задача %s", i)
                await asyncio.gather(child(1), child(2))
                with log_context("Договор.pdf"):
                    log.info("вложенная")
            log.info("снаружи")

        asyncio.run(scenario())
        self.assertEqual(records, ["[prod:7185] задача 1", "[prod:7185] задача 2",
                                   "[prod:7185 / Договор.pdf] вложенная", "снаружи"])


if __name__ == "__main__":
    unittest.main()

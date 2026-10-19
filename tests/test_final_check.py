"""Итоговая проверка комплекта: проверка извлечённых данных по тексту, сводка, факты, ссылка на ЕИС.

Случаи взяты из экспертизы 7185 (выдуманная НМЦК 1 500 000, «3А-8» вместо «ЗА-8» в сканах, продление срока
допсоглашением, недоступный ЕИС).
"""
import asyncio
import json
import unittest
from datetime import date
from unittest import mock

from evaluate_documents.evidence_check import (
    normalize_number, text_dates, text_numbers, verify_extraction, verify_raw_data,
)
from evaluate_documents.final_digest import compact_input, package_facts
from evaluate_documents.package_view import eis_link_assessment
from tests.stubs import stubbed_import

CONTRACT_TEXT = """ДОГОВОР № ЗА-8/487/25 на выполнение работ по текущему ремонту помещений
2.1. Цена Договора составляет 33 728 698 (Тридцать три миллиона семьсот двадцать восемь тысяч шестьсот девяносто
восемь) рублей 15 копеек. 10.1. Цена Договора может быть снижена по соглашению Сторон.
на основании протокола подведения итогов от «02» сентября 2025 г.
3.1. Срок окончания выполнения работ: по 01.12.2025. Сметная документация.
Заказчик: ТУСУР, ИНН 7021000043, КПП 701701001. Штраф 5 000 (Пять тысяч) рублей 00 копеек."""


def _f(value, context):
    """Сумма в формате извлечения."""
    return {"value": value, "context": context, "evidence": {"fragment": "…", "confidence": 0.9}}


def _d(value, context):
    """Дата в формате извлечения."""
    return {"value": value, "context": context, "evidence": {"fragment": "…", "confidence": 0.9}}


class TextIndexTest(unittest.TestCase):
    """Разбор чисел и дат текста."""

    def test_numbers(self):
        """Разряды через пробел, копейки словами, «-» вместо запятой, «млн», OCR-пробел в разрядах."""
        nums = text_numbers(CONTRACT_TEXT + " Сумма 2076618-96. Всего: 17008 366,85. Лимит 1,5 млн руб.")
        for v in (33728698.15, 2076618.96, 17008366.85, 1500000.0, 5000.0):
            self.assertIn(v, nums)
        self.assertNotIn(1500000.0, text_numbers(CONTRACT_TEXT))

    def test_two_numbers_in_a_row_not_merged(self):
        """Соседние числа таблицы КС-3 не склеиваются."""
        nums = text_numbers("в том числе: 5 363 099,50 5 363 099,50 3 632 583,70")
        self.assertIn(5363099.5, nums)
        self.assertIn(3632583.7, nums)

    def test_dates(self):
        """Цифрами, с годом из двух цифр, ISO, словами."""
        found = text_dates("от 15.09.2025; дата 15 09 25; 2025-09-02 10:58; «23» декабря 2025 г.; 7 октября 2025")
        for dt in (date(2025, 9, 15), date(2025, 9, 2), date(2025, 12, 23), date(2025, 10, 7)):
            self.assertIn(dt, found)

    def test_normalize_number(self):
        """«ЗА-8» (буква) и «3А-8» (цифра, OCR) — один номер; другой номер отличается."""
        self.assertEqual(normalize_number("ЗА-8/487/25"), normalize_number("3А-8/487/25"))
        self.assertEqual(normalize_number("№ ЗА-8/487/25"), normalize_number("3A-8/487/25"))
        self.assertNotEqual(normalize_number("ЗА-8/487/25"), normalize_number("ЗА-6/379/25"))


class VerifyRawDataTest(unittest.TestCase):
    """Отбрасывание значений, которых нет в тексте."""

    def test_hallucinations_dropped_real_kept(self):
        """Выдуманные НМЦК, даты, ИНН и метод НМЦК убраны; цена, срок, штраф, настоящий ИНН остались."""
        raw = {
            "contract_number": {"value": "3А-8/487/25", "evidence": {}},
            "finances": [_f(33728698.15, "Цена договора"), _f(1500000.0, "НМЦК"), _f(5000.0, "Штраф"),
                         _f("12 500 000,00", "Стоимость контракта")],
            "dates": [_d("2025-12-01", "Окончание работ"), _d("2023-10-15", "Дата подписания"),
                      _d("02.09.2025", "Протокол"), _d("с даты заключения", "Начало")],
            "organizations": [{"name": "ТУСУР", "role": "заказчик", "inn": "7021000043", "kpp": "701701001",
                               "ogrn": "1027000000000"},
                              {"name": "ООО «Горизонт»", "role": "заказчик", "inn": "1410000000"}],
            "nmck_method": {"method": "среднерыночный", "assessment": "…"},
            "reporting_documentation": [{"document_name": "Акт КС-2", "deadline": "2024-10-15"},
                                        {"document_name": "Справка КС-3", "deadline": "в течение 10 дней"}],
            "summary": "Договор подряда",
        }
        out, dropped = verify_raw_data(raw, CONTRACT_TEXT)
        self.assertEqual([f["value"] for f in out["finances"]], [33728698.15, 5000.0])
        self.assertEqual([d["value"] for d in out["dates"]], ["2025-12-01", "02.09.2025", "с даты заключения"])
        self.assertEqual(out["contract_number"]["value"], "3А-8/487/25")   # З/3 — тот же номер
        self.assertEqual(out["organizations"][0]["inn"], "7021000043")
        self.assertNotIn("ogrn", out["organizations"][0])
        self.assertNotIn("inn", out["organizations"][1])
        self.assertEqual(out["organizations"][1]["name"], "ООО «Горизонт»")   # сама сторона остаётся
        self.assertNotIn("nmck_method", out)
        self.assertNotIn("deadline", out["reporting_documentation"][0])
        self.assertEqual(out["reporting_documentation"][1]["deadline"], "в течение 10 дней")
        self.assertEqual(out["summary"], "Договор подряда")
        self.assertTrue(any("1500000" in x for x in dropped))
        self.assertEqual(raw["finances"][1]["value"], 1500000.0)   # исходные данные не меняются

    def test_contract_number_absent(self):
        """Номер, которого нет в тексте, убирается."""
        out, _ = verify_raw_data({"contract_number": {"value": "123/45"}}, CONTRACT_TEXT)
        self.assertNotIn("contract_number", out)

    def test_nmck_method_kept_when_in_text(self):
        """Метод НМЦК остаётся, если документ его называет."""
        text = "Начальная (максимальная) цена договора определена проектно-сметным методом: 33 898 189,10"
        out, dropped = verify_raw_data({"nmck_method": {"method": "проектно-сметный"},
                                        "finances": [_f(33898189.1, "НМЦК")]}, text)
        self.assertEqual(dropped, [])
        self.assertEqual(out["nmck_method"]["method"], "проектно-сметный")

    def test_verify_extraction_passthrough(self):
        """Без отброшенного — тот же объект; не словарь и пустой текст — как есть."""
        ex = {"raw_data": {"finances": [_f(5000.0, "Штраф")]}, "type_compliance": {"status": "allow"}}
        self.assertIs(verify_extraction(ex, CONTRACT_TEXT), ex)
        self.assertEqual(verify_extraction("строка", CONTRACT_TEXT), "строка")
        self.assertIs(verify_extraction({"raw_data": {"finances": [_f(1.0, "x")]}}, "")["raw_data"]["finances"][0]["value"], 1.0)
        bad = {"raw_data": {"finances": [_f(1500000.0, "НМЦК")]}, "readability": {"status": "allow"}}
        out = verify_extraction(bad, CONTRACT_TEXT, "договор.docx")
        self.assertEqual(out["raw_data"]["finances"], [])
        self.assertEqual(out["readability"], {"status": "allow"})


def _package():
    """Комплект как у экспертизы 7185 (после проверки по тексту)."""
    def raw(filename, **kw):
        """raw_data файла."""
        return dict({"filename": filename, "document_name": {"value": filename}}, **kw)
    return {
        "object": "Отчетные документы по контракту/договору",
        "contract_number": "ЗА-8/487/25",
        "missed_documents": ["Документы, подтверждающие исполнение всех условий, предусмотренных контрактом"],
        "documents": [
            {"doc_code": "linkDocs", "doc_type": "Ссылка на ЕИС", "required": "Обязательный",
             "raw_data": [raw("https://zakupki.gov.ru/…", eis_status="unavailable", eis_error="Не найден archiveUrl")]},
            {"doc_code": "docContractDoWorkFiles", "doc_type": "Контракт", "required": "Обязательный",
             "completeness": {"status": "allow", "description": "ok"},
             "raw_data": [raw("ДОГОВОР.docx", contract_number={"value": "ЗА-8/487/25"},
                              finances=[_f(33728698.15, "Цена договора")],
                              dates=[_d("2025-12-01", "Дата окончания выполнения работ")],
                              summary="Договор подряда на ремонт " * 40)]},
            {"doc_code": "docDocPriemActSdachFiles", "doc_type": "Акты", "required": "Обязательный",
             "raw_data": [raw("КС-3-15.12.pdf", contract_number={"value": "3А-8/487/25"},
                              dates=[_d("2025-12-15", "Дата составления справки о стоимости выполненных работ"),
                                     _d("2025-12-23", "Дата составления протокола заседания комиссии по приемке"),
                                     _d("2025-09-15", "Дата заключения договора")]),
                          raw("КС-2-07.10.pdf", contract_number={"value": "3А-8/487/25"},
                              dates=[_d("07.10.2025", "Дата составления акта")])]},
            {"doc_code": "docDopConsentContractFiles", "doc_type": "ДС", "required": "Необязательный",
             "raw_data": [raw("ДС-1.pdf", contract_number={"value": "3А-8/487/25"},
                              dates=[_d("2025-12-15", "Срок окончания выполнения работ")])]},
            {"doc_code": "docDopMaterialsFiles", "doc_type": "Дополнительные материалы", "required": "Необязательный",
             "raw_data": [raw("Протокол-подведения-итогов.pdf",
                              finances=[_f(33898189.1, "Начальная (максимальная) цена договора"),
                                        _f(33728698.15, "Ценовое предложение участника")]),
                          raw("СЗ-на-ДС.pdf", contract_number={"value": "3А-6/379/25"}),
                          raw("платежка-1.pdf", contract_number={"value": "ЗА-8/487/25"},
                              finances=[_f(20000000.0, "Сумма платежа")]),
                          raw("платежка-2.pdf", finances=[_f(14140130.83, "Сумма платежа")])]},
        ],
    }


class PackageFactsTest(unittest.TestCase):
    """Сверка комплекта кодом."""

    def test_facts_7185(self):
        """Номер совпадает с учётом OCR, цена ниже НМЦК, срок продлён ДС, акт в срок, переплата видна, ЕИС не проверен."""
        facts = package_facts(_package(), today=date(2026, 10, 19))
        self.assertIn("совпадает", facts["номер договора"])   # СЗ с другим договором в «доп. материалах» не считается
        self.assertEqual(facts["цена договора"], "33 728 698,15")
        self.assertEqual(facts["НМЦК"], "33 898 189,10")
        self.assertEqual(facts["цена и НМЦК"], "цена не превышает НМЦК")
        self.assertEqual(facts["срок окончания работ по договору"], "2025-12-01")
        self.assertEqual(facts["срок окончания работ по допсоглашению"], "2025-12-15")
        self.assertEqual(facts["последний акт/справка о выполненных работах"], "2025-12-15")
        self.assertIn("не позже срока", facts["сроки"])
        self.assertIn("больше цены договора на 411 432,68", facts["платёжные поручения"])
        self.assertIn("не проверено", facts["ЕИС"])

    def test_real_mismatch_and_overprice(self):
        """Другой номер в акте и цена выше НМЦК — видны; акт после срока — возможная просрочка."""
        pkg = _package()
        pkg["documents"][2]["raw_data"][1]["contract_number"] = {"value": "ЗА-9/100/25"}
        pkg["documents"][4]["raw_data"][0]["finances"][0]["value"] = 30000000.0
        pkg["documents"][3]["raw_data"] = []   # без допсоглашения срок 01.12
        facts = package_facts(pkg, today=date(2026, 10, 19))
        self.assertIn("КС-2-07.10.pdf: ЗА-9/100/25", facts["номер договора"])
        self.assertIn("превышает НМЦК на 3 728 698,15", facts["цена и НМЦК"])
        self.assertIn("возможна просрочка", facts["сроки"])

    def test_no_data_no_facts(self):
        """Нет сведений — нет фактов (кроме отсутствия НМЦК при известной цене)."""
        self.assertEqual(package_facts({"documents": []}), {})


class CompactInputTest(unittest.TestCase):
    """Сводка для модели."""

    def test_digest_has_all_files_without_evidence(self):
        """Все файлы в сводке, без evidence; при малом пределе подробность снижается, а файлы не теряются."""
        digest, level = compact_input(_package())
        text = json.dumps(digest, ensure_ascii=False)
        self.assertEqual(level, 0)
        self.assertNotIn("evidence", text)
        self.assertIn("платежка-2.pdf", text)
        self.assertIn("Цена договора: 33 728 698,15", text)
        small, level = compact_input(_package(), max_tokens=200)
        self.assertGreater(level, 0)
        small_text = json.dumps(small, ensure_ascii=False)
        self.assertIn("платежка-2.pdf", small_text)
        self.assertNotIn("summary", small_text)


class EisLinkAssessmentTest(unittest.TestCase):
    """Ссылка на ЕИС без модели."""

    def test_unavailable(self):
        """Недоступный ЕИС: один deny в полноте, понятная причина."""
        res = eis_link_assessment([{"raw_data": {"eis_status": "unavailable", "eis_error": "Не найден archiveUrl"}}])
        self.assertEqual(res["type_compliance"]["status"], "allow")
        self.assertEqual(res["readability"]["status"], "allow")
        self.assertEqual(res["completeness"]["status"], "deny")
        self.assertIn("Не найден archiveUrl", res["completeness"]["description"])
        self.assertEqual(len(res["raw_data"]), 1)

    def test_available_goes_to_model(self):
        """Доступная ссылка (или хотя бы одна из нескольких) — оценивает модель."""
        self.assertIsNone(eis_link_assessment([{"raw_data": {"eis_status": "available"}}]))
        self.assertIsNone(eis_link_assessment([{"raw_data": {"eis_status": "available"}},
                                               {"raw_data": {"eis_status": "unavailable"}}]))
        self.assertIsNone(eis_link_assessment([]))


class ConsistencyCheckerInputTest(unittest.TestCase):
    """Модель получает факты и сводку всего комплекта, а не обрезанный JSON."""

    def test_message_contains_facts_and_last_documents(self):
        """В сообщении — факты и последний файл комплекта даже при большом входе."""
        pkg = _package()
        pkg["documents"][1]["raw_data"][0]["summary"] = "длинный текст " * 20000   # раньше обрезало хвост
        sent = {}

        async def fake_call_llm(client, **kwargs):
            """Запоминает сообщения, отвечает валидным JSON."""
            sent.update(kwargs)
            return json.dumps({"overall_summary": "ok", "overall_status": "deny"})

        with stubbed_import("evaluate_documents.consistency_checker") as cc:
            with mock.patch.object(cc, "call_llm", fake_call_llm):
                checker = cc.ConsistencyChecker(None, "m")
                result = asyncio.run(checker.check_consistency(pkg))
        user = sent["messages"][1]["content"]
        self.assertIn("ФАКТЫ", user)
        self.assertIn("цена не превышает НМЦК", user)
        self.assertIn("платежка-2.pdf", user)
        self.assertNotIn("длинный текст " * 30, user)   # описание файла сокращено, а не весь вход обрезан
        self.assertEqual(result["overall_status"], "deny")
        self.assertEqual(len(result["documents"]), 5)


if __name__ == "__main__":
    unittest.main()

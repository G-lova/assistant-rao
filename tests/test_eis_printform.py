"""Тесты разбора печатной формы извещения ЕИС (раздел 1 заключения без XML)."""
import unittest
from pathlib import Path

from knowledge_store import eis_notice, eis_printform, facts

DATA = Path(__file__).parent / "data"
NOTICE = (DATA / "printform_notice.txt").read_text(encoding="utf-8")
CORRECTION = (DATA / "printform_correction.txt").read_text(encoding="utf-8")


class PrintFormParseTests(unittest.TestCase):
    """Разбор меток и разделов."""

    def test_detects_notice_only(self):
        """Печатная форма извещения распознаётся, форма исправления — нет."""
        self.assertTrue(eis_printform.is_print_form(NOTICE))
        self.assertFalse(eis_printform.is_print_form(CORRECTION))
        self.assertFalse(eis_printform.is_print_form("обычный документ"))

    def test_labels_and_values(self):
        """Значения меток собираются из нескольких строк; «Информация отсутствует» — пусто."""
        form = eis_printform.PrintForm(NOTICE)
        self.assertEqual(form.value("email"), "UsatovaTM@mpei.ru")
        self.assertIsNone(form.value("fax"))
        self.assertTrue(form.has("fax"))
        self.assertIn("НАЦИОНАЛЬНЫЙ ИССЛЕДОВАТЕЛЬСКИЙ УНИВЕРСИТЕТ", form.value("org"))
        self.assertIn("в соответствии с 44-ФЗ", form.value("app_order"))      # метка на двух строках

    def test_label_not_section(self):
        """«Обеспечение гарантийных обязательств не требуется» — метка, а не заголовок раздела."""
        form = eis_printform.PrintForm(NOTICE)
        self.assertTrue(form.has("warranty_not_required"))
        self.assertIn("Обеспечение гарантийных обязательств", form.sections)


class PrintFormRulesTests(unittest.TestCase):
    """Правила критериев раздела 1."""

    @classmethod
    def setUpClass(cls):
        cls.found = eis_printform.evaluate_text(NOTICE)

    def value(self, number):
        finding = self.found.get(number)
        return finding.value if finding else None

    def test_contacts(self):
        """Контакты (1.4, 1.5, 1.6) найдены с доказательством «метка = значение»."""
        for number in ("1.4", "1.5", "1.6"):
            self.assertEqual(self.value(number), 1)
        self.assertEqual(self.found["1.4"].evidence, "Адрес электронной почты = UsatovaTM@mpei.ru")

    def test_not_provided(self):
        """Нет специализированной организации, этапов, аванса, преимуществ, национального режима — «2»."""
        for number in ("1.7", "1.17", "1.19", "1.22", "1.28", "1.29", "1.30", "1.39"):
            self.assertEqual(self.value(number), 2, number)

    def test_dates_price_ikz(self):
        """Даты, цена, ИКЗ, валюта, критерии, обеспечение."""
        for number in ("1.8", "1.18", "1.21", "1.23", "1.24", "1.31", "1.34", "1.41", "1.44", "1.45", "1.46", "1.40"):
            self.assertEqual(self.value(number), 1, number)
        self.assertIsNone(self.value("1.42"))          # первых частей нет — решает эксперт
        self.assertIsNone(self.value("1.38"))          # «не требуется» эксперты оценивают по-разному

    def test_empty_values_are_absent(self):
        """Метка есть, значение «Информация отсутствует» → «0»."""
        text = NOTICE.replace("UsatovaTM@mpei.ru", "Информация отсутствует")
        self.assertEqual(eis_printform.evaluate_text(text)["1.4"].value, 0)

    def test_regulation_reference_left_to_expert(self):
        """Порядок обеспечения со ссылкой на регламент площадки не решается."""
        text = NOTICE.replace("в соответствии с 44-ФЗ", "в соответствии с регламентом электронной площадки", 1)
        self.assertNotIn("1.32", eis_printform.evaluate_text(text))

    def test_correction_form_ignored(self):
        """По форме исправления критерии не оцениваются."""
        self.assertEqual(eis_printform.evaluate_text(CORRECTION), {})


class PrintFormFactsTests(unittest.TestCase):
    """Факты по печатной форме и их сопоставление с полями формы."""

    FIELDS = [{"field_key": "field2_1_4", "label": "1.4. Наличие информации об адресе электронной почты", "value_kind": "presence"},
              {"field_key": "field2_1_6", "label": "1.6. Наличие информации об ответственном должностном лице", "value_kind": "presence"},
              {"field_key": "field2_1_7", "label": "1.7. Наличие информации о специализированной организации", "value_kind": "presence"}]

    def test_facts_and_xml_priority(self):
        """Факты источника eis_xml с пометкой origin; критерии, решённые по XML, не перезаписываются."""
        docs = [{"id": 5, "filename": "Печатная-форма-извещения-(версия-1).html", "text_full": NOTICE},
                {"id": 6, "filename": "Печатная-форма-исправления.html", "text_full": CORRECTION}]
        out = facts.build_printform_facts(docs, self.FIELDS, solved=["field2_1_6"])
        keys = {f["fact_key"]: f for f in out}
        self.assertEqual(set(keys), {"field2_1_4", "field2_1_7"})
        self.assertEqual(keys["field2_1_4"]["value"]["value"], 1)
        self.assertEqual(keys["field2_1_4"]["value"]["origin"], "print_form")
        self.assertEqual(keys["field2_1_4"]["document_id"], 5)
        self.assertEqual(keys["field2_1_7"]["value"]["value"], 2)

    def test_latest_version_chosen(self):
        """Из нескольких версий берётся последняя."""
        newer = NOTICE.replace("UsatovaTM@mpei.ru", "new@mail.ru")
        docs = [{"id": 1, "filename": "Печатная-форма-извещения-(версия-1).html", "text_full": NOTICE},
                {"id": 2, "filename": "Печатная-форма-извещения-(версия-2).html", "text_full": newer}]
        out = facts.build_printform_facts(docs, self.FIELDS)
        self.assertIn("new@mail.ru", {f["fact_key"]: f for f in out}["field2_1_4"]["quote"])

    def test_no_print_form(self):
        """Без печатной формы фактов нет."""
        self.assertEqual(facts.build_printform_facts([{"id": 1, "filename": "x", "text_full": "текст"}], self.FIELDS), [])


class XmlNewRulesTests(unittest.TestCase):
    """Правила 1.28 и 1.30 по XML: отсутствие блоков → «не предусмотрено»."""

    XML = ("<export><epNotificationEOK2020><notificationInfo><purchaseObjectsInfo><notDrugPurchaseObjectsInfo>"
           "<purchaseObject><name>x</name></purchaseObject></notDrugPurchaseObjectsInfo></purchaseObjectsInfo>"
           "</notificationInfo></epNotificationEOK2020></export>")

    def test_absent_blocks(self):
        """Нет preferensesInfo и restrictionsInfo → 2."""
        notice = eis_notice.Notice.from_xml(self.XML)
        found = eis_notice.evaluate_all(notice)
        self.assertEqual(found["1.28"].value, 2)
        self.assertEqual(found["1.30"].value, 2)

    def test_restrictions_present_left_to_llm(self):
        """Блок restrictionsInfo есть — правило не решает."""
        xml = self.XML.replace("<name>x</name>", "<name>x</name><restrictionsInfo><isRestrictForeignInfo>true</isRestrictForeignInfo></restrictionsInfo>")
        found = eis_notice.evaluate_all(eis_notice.Notice.from_xml(xml))
        self.assertIsNone(found["1.30"].value)


if __name__ == "__main__":
    unittest.main()


class XmlSafetyTests(unittest.TestCase):
    """Безопасный разбор внешнего XML."""

    def test_entity_bomb_rejected(self):
        """XML с объявлением сущностей отклоняется (ValueError), а не раскрывается."""
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                "<export><epNotificationEOK2020>&b;</epNotificationEOK2020></export>")
        with self.assertRaises(ValueError):
            eis_notice.Notice.from_xml(bomb)

    def test_text_placeholders(self):
        """Заглушки «отсутствует», «нет», нули не считаются значением."""
        for text in ("000000000", "Информация отсутствует", "нет", "-"):
            self.assertTrue(eis_notice._is_placeholder(text), text)
        self.assertFalse(eis_notice._is_placeholder("vlad@mail.ru"))

"""Тесты правил критерия «описание объекта закупки» (``knowledge_store.monitoring_object``)."""

import unittest

from knowledge_store import monitoring_object as mo

# Полный договор об охране: предмет, лицензия, приёмка по акту, оплата в срок
FULL = ("Предмет договора: оказание услуг по охране объекта. Исполнитель обязан иметь действующую лицензию на частную "
        "охранную деятельность. Приёмка услуг оформляется актом в течение 5 рабочих дней после окончания месяца. Заказчик оплачивает услуги в течение 10 дней. "
        ) * 30


class ContractRulesTest(unittest.TestCase):
    def test_full_contract_is_compliant(self):
        self.assertEqual(mo.check_contract(FULL)["verdict"], "Соответствует")

    def test_no_acceptance_terms(self):
        text = ("Предмет договора: оказание услуг по охране. Исполнитель обязан иметь действующую лицензию на охранную деятельность. "
                "Заказчик оплачивает услуги в течение 10 дней. ") * 40
        r = mo.check_contract(text)
        self.assertEqual([f["rule"] for f in r["findings"]], ["NO_ACCEPTANCE_TERMS"])
        self.assertTrue(r["findings"][0]["quote"].startswith("Предмет договора"))

    def test_no_license_for_security(self):
        text = FULL.replace("лицензию", "документ")
        self.assertIn("NO_LICENSE_REQUIREMENT", [f["rule"] for f in mo.check_contract(text)["findings"]])

    def test_license_rule_skipped_for_ocr(self):
        """Для распознанного скана правило лицензии не применяется (искажённые слова)."""
        text = FULL.replace("лицензию", "документ")
        self.assertNotIn("NO_LICENSE_REQUIREMENT", [f["rule"] for f in mo.check_contract(text, ocr=True)["findings"]])

    def test_acceptance_without_term_is_violation(self):
        """Упоминание документа о приёмке без срока — нарушение (как в ЭЗ по Ингушскому ГУ)."""
        text = FULL.replace("в течение 5 рабочих дней", "по окончании периода")
        self.assertIn("NO_ACCEPTANCE_TERMS", [f["rule"] for f in mo.check_contract(text)["findings"]])

    def test_short_text_is_insufficient(self):
        self.assertEqual(mo.check_contract("Договор охраны")["verdict"], "Недостаточно данных")

    def test_eis_stub_is_insufficient(self):
        r = mo.check_contract("Электронная версия контракта по закупке №1 " + "реквизиты " * 600)
        self.assertEqual(r["verdict"], "Недостаточно данных")


if __name__ == "__main__":
    unittest.main()

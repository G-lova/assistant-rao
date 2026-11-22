"""Тесты правил проверки обоснования НМЦК (``knowledge_store.monitoring_nmck``) на синтетических текстах."""
import unittest

from knowledge_store import monitoring_nmck as nm

ORDER45_COMBINED = ("ОБОСНОВАНИЕ НАЧАЛЬНОЙ (МАКСИМАЛЬНОЙ) ЦЕНЫ КОНТРАКТА по Приказу Росгвардии от 15.02.2021 № 45. "
                    "Направлены запросы о предоставлении информации о стоимости услуг. Коммерческие предложения не получены. "
                    "Осуществлен поиск в реестре контрактов. Дата подготовки обоснования: 18.11.2024. Специалист Иванов")
CALC_ONLY = "Обоснование начальной (максимальной) цены договора. Расчет подготовлен согласно Приказу Росгвардии от 15.02.2021 № 45."


class MonitoringNmckTest(unittest.TestCase):
    def test_skip_by_name(self):
        self.assertTrue(nm.should_skip_by_name("Контракт_охрана"))
        self.assertTrue(nm.should_skip_by_name("платежные поручения"))
        self.assertFalse(nm.should_skip_by_name("3. 2) Расчет и КП"))      # «договор» не в имени, но есть признак КП
        self.assertFalse(nm.should_skip_by_name("Договор 7. Обоснование НМЦК 1121"))

    def test_classify_by_name_and_text(self):
        self.assertEqual(nm.classify(nm.Doc("Обоснование НМЦК 106.pdf", "")), nm.DOC_NMCK)
        self.assertEqual(nm.classify(nm.Doc("КП1.pdf", "")), nm.DOC_OFFER)
        self.assertEqual(nm.classify(nm.Doc("скан1.pdf", "ОБОСНОВАНИЕ НАЧАЛЬНОЙ (МАКСИМАЛЬНОЙ) ЦЕНЫ")), nm.DOC_NMCK)

    def test_no_justification_with_offers_is_violation(self):
        r = nm.check_purchase([nm.Doc("Коммерческие май.pdf", "текст"), nm.Doc("Контракт.pdf", None)])
        self.assertEqual(r["verdict"], "Не соответствует")
        self.assertEqual(r["findings"][0]["rule"], "NO_JUSTIFICATION")

    def test_empty_complect_is_insufficient(self):
        self.assertEqual(nm.check_purchase([nm.Doc("Контракт.pdf", None)])["verdict"], "Недостаточно данных")

    def test_combined_methods_order45(self):
        r = nm.check_purchase([nm.Doc("Обоснование НМЦК.pdf", ORDER45_COMBINED)])
        self.assertEqual(r["verdict"], "Не соответствует")
        self.assertEqual(r["findings"][0]["rule"], "COMBINED_METHODS_ORDER45")
        # цитата — подряд идущие слова текста
        self.assertIn(r["findings"][0]["quote"][:30], nm.norm(ORDER45_COMBINED))

    def test_formula_calculation_without_author_is_compliant(self):
        r = nm.check_purchase([nm.Doc("Расчет и КП.pdf", CALC_ONLY)])
        self.assertEqual(r["verdict"], "Соответствует")
        self.assertEqual(r["notes"][0]["rule"], "NO_AUTHOR_DATE")      # только пометка, вердикт не меняет


if __name__ == "__main__":
    unittest.main()

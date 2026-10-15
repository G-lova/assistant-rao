"""Тесты вспомогательных функций скрипта оценки ``scripts.eval_generated``."""
import unittest

from scripts.eval_generated import funding_categories, source_label


class EvalHelpersTests(unittest.TestCase):
    """Сравнение источника финансирования по смыслу и подписи источников."""

    def test_funding_categories(self):
        """Одинаковый смысл — одинаковые категории; «внебюджетные» не считаются бюджетом."""
        self.assertEqual(funding_categories("Закупка за счет собственных средств организации"),
                         funding_categories("За счет собственных средств организации"))
        self.assertEqual(funding_categories("За счет внебюджетных средств"), frozenset({"extra"}))
        self.assertEqual(funding_categories("субсидии ГЗ и внебюджет"), frozenset({"subsidy", "extra"}))
        self.assertEqual(funding_categories("Средства бюджетных учреждений"), frozenset({"budget"}))
        self.assertEqual(funding_categories(None), frozenset())

    def test_source_label(self):
        """Источник: source, иначе начало правила, иначе «-»."""
        self.assertEqual(source_label({"source": "eis_xml"}), "eis_xml")
        self.assertTrue(source_label({"rule": "x" * 100}).startswith("rule:"))
        self.assertEqual(source_label({}), "-")

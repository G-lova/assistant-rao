"""Тесты правил раздела 1 по XML извещения 223-ФЗ."""
import unittest
from pathlib import Path

from knowledge_store import eis_notice, eis_notice223, facts, law_context

DATA = Path(__file__).parent / "data"
LABELS = {
    "field2_1_1": "1.1. Наличие информации о наименовании Заказчика",
    "field2_1_6": "1.6. Наличие информации о количестве поставляемого товара, об объеме выполнения работы или оказания услуги",
    "field2_1_7": "1.7. Наличие информации о единице измерения поставки товаров, выполнения работы или оказания услуги",
    "field2_1_11": "1.11. Наличие требования об обеспечении заявки и информации о размере такого обеспечения",
}
FIELDS = [{"field_key": k, "label": v, "value_kind": "presence"} for k, v in LABELS.items()]


def load(name: str) -> eis_notice223.Notice223:
    """Разбирает XML-фикстуру из ``tests/data``."""
    return eis_notice223.Notice223.from_xml((DATA / name).read_bytes())


class Notice223Tests(unittest.TestCase):
    """Правила 1.1–1.10 по реальным извещениям единственного поставщика."""

    def test_all_present(self):
        """Извещение со всеми сведениями: 1.1–1.10 решены как «1» с доказательством-путём."""
        found = eis_notice223.evaluate_all(load("notice223_ep_qty1.xml"))
        for number in ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10"):
            self.assertEqual(found[number].value, 1, number)
        self.assertIn("customer/mainInfo/fullName", found["1.1"].evidence)

    def test_missing_quantity_is_zero(self):
        """Нет количества и единицы измерения в позициях лота → 0 (в извещении этих сведений нет)."""
        found = eis_notice223.evaluate_all(load("notice223_ep_qty0.xml"))
        self.assertEqual((found["1.6"].value, found["1.7"].value), (0, 0))
        self.assertEqual(found["1.1"].value, 1)

    def test_not_a_notice(self):
        """XML 44-ФЗ и посторонний XML извещением 223-ФЗ не считаются."""
        self.assertFalse(eis_notice223.is_notice_223(b"<epNotificationEF2020><a/></epNotificationEF2020>"))
        self.assertFalse(eis_notice223.is_notice_223(b"not xml"))

    def test_44fz_xml_is_not_223(self):
        """XML извещения 223-ФЗ не принимается разбором 44-ФЗ."""
        with self.assertRaises(ValueError):
            eis_notice.Notice.from_xml((DATA / "notice223_ep_qty1.xml").read_bytes())


class MappingTests(unittest.TestCase):
    """Привязка результатов к полям формы 223-ФЗ — по номеру в названии, не по смыслу правил 44-ФЗ."""

    def setUp(self):
        """Результат правил для извещения с количеством."""
        notice = load("notice223_ep_qty1.xml")
        self.stored = eis_notice223.findings_to_json(notice, eis_notice223.evaluate_all(notice))

    def test_map_by_number(self):
        """1.6 формы 223-ФЗ получает результат критерия 1.6; 1.11 (обеспечение заявки) в XML нет — не заполняется."""
        mapped = eis_notice223.map_to_fields(self.stored, FIELDS)
        self.assertEqual(mapped["field2_1_6"].value, 1)
        self.assertNotIn("field2_1_11", mapped)

    def test_44fz_rules_would_misfire(self):
        """Правила 44-ФЗ на названиях 223-ФЗ дали бы чужой критерий (1.6 → 1.13), поэтому для 223-ФЗ они не используются."""
        self.assertEqual(eis_notice.rule_id(LABELS["field2_1_6"]), "1.13")

    def test_build_eis_facts_uses_law_rules(self):
        """``build_eis_facts`` для формы 223-ФЗ берёт правила 223-ФЗ, для формы 44-ФЗ — прежние."""
        notices = [{"id": 1, "eis_notice": self.stored}]
        law_context.set_from_form("223fz_single_supplier_obj6")
        try:
            keys = {f["fact_key"]: f for f in facts.build_eis_facts(notices, FIELDS)}
        finally:
            law_context.set_from_form("44fz_competition_obj6")
        self.assertEqual(keys["field2_1_6"]["value"]["value"], 1)
        self.assertNotIn("field2_1_11", keys)

    def test_hook_patch_for_223_xml(self):
        """Хук загрузки документа разбирает XML 223-ФЗ и сохраняет критерии с признаком закона."""
        from knowledge_store import hooks
        path = str(DATA / "notice223_ep_qty1.xml")
        patch = hooks._eis_notice_patch(path, "notice.xml")
        self.assertEqual(patch["law"], "223-ФЗ")
        self.assertEqual(patch["criteria"]["1.5"]["value"], 1)


if __name__ == "__main__":
    unittest.main()

"""Тесты оверлея названий полей форм."""
import unittest

from knowledge_store import form_labels


class FormLabelsTests(unittest.TestCase):
    """Сопоставление типов форм справочника с кодами сервиса и содержимое оверлея."""

    def test_service_form_codes(self):
        """Типы форм справочника превращаются в коды форм сервиса."""
        self.assertEqual(form_labels.service_form_codes("fz44_tovary", 7), ["44fz_goods_obj7"])
        self.assertEqual(form_labels.service_form_codes("fz223_konkurs", 6), ["223fz_competitive_obj6"])
        self.assertEqual(form_labels.service_form_codes("fz44_ed_postavshchik", 6), ["44fz_single_supplier_obj6"])
        self.assertEqual(form_labels.service_form_codes("unknown", 6), [])

    def test_overlay_has_top_level_labels_only(self):
        """В оверлее нет вложенных ключей (с точкой) и пустых названий."""
        overlay = form_labels.load_overlay()
        self.assertIn("44fz_goods_obj7", overlay)
        for fields in overlay.values():
            for key, label in fields.items():
                self.assertNotIn(".", key)
                self.assertTrue(label.strip())
        self.assertEqual(form_labels.labels_for_form("no_such_form"), {})


if __name__ == "__main__":
    unittest.main()

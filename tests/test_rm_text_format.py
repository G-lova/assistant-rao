"""Оформление заключений ИИ (``risk_monitoring.text_format``)."""
import unittest
from types import SimpleNamespace

from risk_monitoring import text_format as tf

PLAIN = SimpleNamespace(AI_TEXT_FORMAT="plain")
MD = SimpleNamespace(AI_TEXT_FORMAT="markdown")


class NormalizeTest(unittest.TestCase):
    """Приведение ответа модели к формату."""

    def test_markdown_to_plain(self):
        """Заголовок, жирный, маркеры * и 1. → абзацы и пункты «— » без символов разметки."""
        t = "### Итог\nЗакупка в **красной** зоне.\nПризнаки риска:\n* аванс без обеспечения\n1. одна заявка\n\n\nПроверить НМЦК."
        out = tf.normalize(t, PLAIN)
        self.assertEqual(out, "Итог: Закупка в красной зоне.\n\nПризнаки риска:\n— аванс без обеспечения\n— одна заявка"
                              "\n\nПроверить НМЦК.")
        self.assertNotIn("*", out)
        self.assertNotIn("#", out)

    def test_lines_joined_into_paragraph(self):
        """Строки одного абзаца склеиваются, пустые строки сжимаются до одной."""
        self.assertEqual(tf.normalize("Первое.\nВторое.\n\n\n\nТретье.", PLAIN), "Первое. Второе.\n\nТретье.")

    def test_markdown_mode_keeps_bold(self):
        """В режиме markdown — пункты «- », жирный сохраняется."""
        out = tf.normalize("Вывод **важный**.\n— один\n— два", MD)
        self.assertEqual(out, "Вывод **важный**.\n\n- один\n- два")

    def test_dash_in_prose_kept(self):
        """Тире внутри предложения — не пункт списка."""
        self.assertEqual(tf.normalize("Балл — 86, зона — красная.", PLAIN), "Балл — 86, зона — красная.")

    def test_empty(self):
        """Пустой текст — пустая строка."""
        self.assertEqual(tf.normalize(None, PLAIN), "")


class ComposeTest(unittest.TestCase):
    """Шаблонные заключения."""

    def test_compose(self):
        """Вывод, вводная строка со списком, завершающий абзац."""
        self.assertEqual(tf.compose("Вывод.", ["а", "", "б;"], "Хвост.", "Факторы:", PLAIN),
                         "Вывод.\n\nФакторы:\n— а\n— б\n\nХвост.")

    def test_contract_template(self):
        """Шаблонное резюме закупки — абзацами, факторы списком."""
        from risk_monitoring import contract_package as cp

        text = cp.default_resume({"purchase_number": "1", "law": "44-ФЗ", "subject": "Ремонт", "stage": "notice"},
                                 {"score": 72, "zone": "red"}, [{"rule_id": "FIN-003", "title": "Демпинг"}], [])
        self.assertIn("\n\nОсновные факторы:\n— Демпинг", text)

    def test_instruction(self):
        """Инструкция модели зависит от режима."""
        self.assertIn("«— »", tf.instruction(PLAIN))
        self.assertIn("markdown", tf.instruction(MD))


if __name__ == "__main__":
    unittest.main()

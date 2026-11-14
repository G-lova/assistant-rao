"""``configs.fuzzy``: шкала и граничные случаи как у ``fuzzywuzzy`` при любой реализации (rapidfuzz / difflib)."""
import unittest

from configs import fuzzy


class FuzzyTest(unittest.TestCase):
    """``ratio`` / ``partial_ratio``."""

    def test_scale(self):
        """Целые 0–100; одинаковые строки — 100, совсем разные — мало."""
        self.assertEqual(fuzzy.ratio("ООО Ромашка", "ООО Ромашка"), 100)
        self.assertIsInstance(fuzzy.ratio("абв", "абг"), int)
        self.assertLess(fuzzy.ratio("Ромашка", "Тюльпан"), 50)
        self.assertGreaterEqual(fuzzy.ratio("Тульский университет", "Тульский университет им."), 85)

    def test_empty(self):
        """Пустая строка или не строка — 0 (у rapidfuzz две пустые строки дали бы 100)."""
        self.assertEqual(fuzzy.ratio("", ""), 0)
        self.assertEqual(fuzzy.ratio("а", ""), 0)
        self.assertEqual(fuzzy.ratio(None, "а"), 0)
        self.assertEqual(fuzzy.partial_ratio("", "текст"), 0)

    def test_partial(self):
        """Цитата внутри длинного текста — 100; с опечаткой — высокое сходство."""
        text = "поставщик обязан поставить товар в течение 10 дней с даты заключения контракта"
        self.assertEqual(fuzzy.partial_ratio("в течение 10 дней", text), 100)
        self.assertGreaterEqual(fuzzy.partial_ratio("в течении 10 дней", text), 85)
        self.assertIn(fuzzy.BACKEND, ("rapidfuzz", "thefuzz", "fuzzywuzzy", "difflib"))


if __name__ == "__main__":
    unittest.main()

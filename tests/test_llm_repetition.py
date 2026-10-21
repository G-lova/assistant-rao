"""Защита от зацикливания модели при извлечении данных (``configs.llm_repetition``)."""
import unittest

from configs.llm_repetition import dedupe_repeats, penalized


class RepetitionTest(unittest.TestCase):
    """Повторы записей убираются; повторный запрос — со штрафом за повторы."""

    def test_dedupe(self):
        """20 одинаковых сумм с разной уверенностью → одна; разные записи остаются."""
        item = {"value": 1799686.68, "context": "Средняя арифметическая цена за единицу",
                "evidence": {"fragment": "Средняя арифметическая цена", "confidence": 0.9}}
        finances = [dict(item, evidence=dict(item["evidence"], confidence=0.9 - i / 100)) for i in range(20)]
        finances.append({"value": 196411.42, "context": "НДС, руб.", "evidence": {"fragment": "НДС"}})
        result, removed = dedupe_repeats({"readability": {}, "raw_data": {"finances": finances, "summary": "x"}})
        self.assertEqual(removed, 19)
        self.assertEqual([f["value"] for f in result["raw_data"]["finances"]], [1799686.68, 196411.42])

    def test_penalized(self):
        """Штраф добавляется, guided_json и прочие параметры сохраняются."""
        req = penalized({"model": "m", "max_tokens": 3000, "extra_body": {"guided_json": {"type": "object"}}})
        self.assertEqual(req["frequency_penalty"], 0.4)
        self.assertEqual(req["extra_body"], {"guided_json": {"type": "object"}, "repetition_penalty": 1.1})
        self.assertEqual(req["max_tokens"], 3000)


if __name__ == "__main__":
    unittest.main()

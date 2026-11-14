"""``configs.utils.split_large_text``: нарезка по абзацам/предложениям без слипания абзацев, лимит в токенах.

tiktoken в песочнице может отсутствовать — подставляется энкодер «одно слово = один токен».
"""
import unittest


class WordEncoder:
    """Энкодер для тестов: токен — слово или разделитель (пробельная последовательность)."""

    def __init__(self):
        self.vocab, self.words = {}, []
        self.calls = 0

    def encode(self, text, disallowed_special=None):
        """Кодирует текст; считает вызовы."""
        import re

        self.calls += 1
        out = []
        for w in re.findall(r"\S+|\s+", text):
            if w not in self.vocab:
                self.vocab[w] = len(self.words)
                self.words.append(w)
            out.append(self.vocab[w])
        return out

    def decode(self, tokens):
        """Декодирует токены."""
        return "".join(self.words[t] for t in tokens)


class SplitLargeTextTest(unittest.TestCase):
    """Нарезка текста на фрагменты."""

    def split(self, text, size):
        """Вызывает ``split_large_text`` с тестовым энкодером.

        Returns:
            Tuple[list, WordEncoder]: Фрагменты и энкодер.
        """
        from tests.stubs import stubbed_import

        with stubbed_import("configs.utils") as utils:
            enc = WordEncoder()
            utils._ENCODER = enc
            return utils.split_large_text(text, max_chunk_size=size), enc

    def test_short_text_as_is(self):
        """Короткий текст — один фрагмент, без изменений."""
        chunks, _ = self.split("Один абзац.\r\n\r\nВторой абзац.", 100)
        self.assertEqual(chunks, ["Один абзац.\r\n\r\nВторой абзац."])

    def test_paragraphs_not_glued(self):
        """Абзацы внутри фрагмента разделены пустой строкой (раньше слипались)."""
        text = "\n\n".join(f"Абзац номер {i} текста." for i in range(10))
        chunks, _ = self.split(text, 20)
        self.assertGreater(len(chunks), 1)
        self.assertIn("Абзац номер 0 текста.\n\nАбзац номер 1 текста.", chunks[0])
        for c in chunks:
            self.assertNotIn("текста.Абзац", c)
        # ничего не потеряно
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_limit_respected(self):
        """Каждый фрагмент не длиннее лимита в токенах."""
        text = "\n\n".join("слово " * (i % 7 + 1) + "конец." for i in range(40))
        chunks, enc = self.split(text, 15)
        for c in chunks:
            self.assertLessEqual(len(enc.encode(c)), 15, c)

    def test_long_paragraph_by_sentences(self):
        """Абзац больше лимита — по предложениям, предложения через пробел."""
        para = " ".join(f"Предложение {i} здесь." for i in range(12))
        chunks, enc = self.split(para, 12)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("Предложение 0 здесь. Предложение 1 здесь."))
        self.assertEqual(" ".join(chunks).split(), para.split())

    def test_giant_sentence_hard_cut(self):
        """Предложение больше лимита режется по токенам."""
        text = "а " * 50
        chunks, enc = self.split(text, 10)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks).count("а"), 50)

    def test_single_encoding_per_paragraph(self):
        """Обычные абзацы кодируются по одному разу (+1 вызов для разделителя), весь текст отдельно — нет."""
        text = "\n\n".join(f"Абзац {i}." for i in range(30))
        _, enc = self.split(text, 10)
        self.assertEqual(enc.calls, 30 + 1)

    def test_first_chunk_lazy(self):
        """``first_text_chunk`` совпадает с первым фрагментом и не кодирует весь текст."""
        from tests.stubs import stubbed_import

        text = "\n\n".join(f"Абзац {i}." for i in range(200))
        full, _ = self.split(text, 10)
        with stubbed_import("configs.utils") as utils:
            enc = WordEncoder()
            utils._ENCODER = enc
            first = utils.first_text_chunk(text, 10)
        self.assertEqual(first, full[0])
        self.assertLess(enc.calls, 20)

    def test_empty(self):
        """Пустой текст и текст из пробелов — пустой список."""
        self.assertEqual(self.split("", 10)[0], [])
        self.assertEqual(self.split("\n\n  \n\n", 10)[0], [])


if __name__ == "__main__":
    unittest.main()

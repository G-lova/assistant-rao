"""Тесты разбора ссылок внутреннего хранилища ``/api/media/<id>``.

Исходник ``configs.parsing`` тянет playwright/aiohttp, которых может не быть в окружении тестов,
поэтому функция достаётся из текста модуля.
"""
import re
import textwrap
import unittest
from typing import Tuple
from urllib.parse import parse_qs, unquote, urlparse


def _load():
    """Загружает ``CloudStorageParser._split_media_url`` без импорта тяжёлых зависимостей."""
    src = open("configs/parsing.py", encoding="utf-8").read()
    i = src.index("    @staticmethod\n    def _split_media_url")
    j = src.index("    @async_retry", i)
    ns = {"re": re, "urlparse": urlparse, "unquote": unquote, "parse_qs": parse_qs, "Tuple": Tuple}
    exec(textwrap.dedent(src[i:j]).replace("@staticmethod\n", ""), ns)
    return ns["_split_media_url"]


class MediaUrlTests(unittest.TestCase):
    """Проверки ``_split_media_url``."""

    def test_forms(self):
        """Ссылка без имени, с именем в пути и в query даёт верный адрес скачивания."""
        f = _load()
        base = "https://stage.rao0123.1t.ws/api/media/25739"
        self.assertEqual(f(base), (base, ""))
        self.assertEqual(f(base + "/%D0%A4.pdf"), (base, "Ф.pdf"))
        self.assertEqual(f(base + "?filename=a.docx"), (base, "a.docx"))


if __name__ == "__main__":
    unittest.main()

"""Проверка, что все SQL-файлы, на которые ссылается код, лежат в каталоге ``queries/``."""
import re
import subprocess
import unittest
from pathlib import Path

from configs.config import Config

ROOT = Path(__file__).resolve().parent.parent


class SqlFilesTests(unittest.TestCase):
    """Согласованность путей к SQL-запросам."""

    def test_default_path_is_queries(self):
        """Путь по умолчанию — ``queries/`` (так принято в проекте)."""
        self.assertEqual(Config.SQL_QUERIES_PATH.rstrip("/"), "queries")

    def test_referenced_sql_files_exist(self):
        """Каждое имя ``*.sql`` из кода (кроме миграций) существует в ``queries/``."""
        files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        names = set()
        for f in files:
            if f.startswith(("tests/", "scripts/", "migrations/", "knowledge_store/")):
                continue
            names |= set(re.findall(r"""["']([\w\-]+\.sql)["']""", (ROOT / f).read_text(encoding="utf-8")))
        names -= {"get_experts.sql", "init.sql"}  # примеры в docstring
        self.assertTrue(names, "в коде не найдено ссылок на SQL-файлы")
        for name in names:
            self.assertTrue((ROOT / "queries" / name).exists(), f"нет queries/{name}")

    def test_no_sql_left_in_configs(self):
        """В ``configs/`` не осталось SQL-файлов."""
        self.assertEqual(list((ROOT / "configs").glob("*.sql")), [])


if __name__ == "__main__":
    unittest.main()

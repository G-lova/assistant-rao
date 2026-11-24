"""SQL-запросы портала: строчные комментарии не обрезают запрос при склейке в одну строку.

24.11.2026: комментарий «-- MIME-тип …» в ``evaluate_docs_script.sql`` после склейки закомментировал хвост запроса
(``… WHERE e.id = ?``) — портал отвечал 500 «SQL syntax … near ''».
"""
import glob
import os
import unittest


def utils():
    from tests.stubs import stubbed_import

    with stubbed_import("configs.utils") as module:
        return module


class SqlCommentsTest(unittest.TestCase):
    def test_strip(self):
        u = utils()
        sql = ("SELECT a, -- удалить1\n"
               "  '--не комментарий', \"#тоже\", `col--x`, 'it\\'s -- ok' # хвост\n"
               "FROM t WHERE x LIKE '%Expertise' AND y = ?;")
        out = u.normalize_sql(sql)
        self.assertNotIn("удалить1", out)
        self.assertNotIn("хвост", out)
        for keep in ("'--не комментарий'", '"#тоже"', "`col--x`", "'it\\'s -- ok'", "WHERE x LIKE '%Expertise' AND y = ?;"):
            self.assertIn(keep, out)

    def test_all_queries_keep_tail(self):
        """Каждый файл queries/*.sql после нормализации кончается тем же, чем исходный текст (без комментариев)."""
        u = utils()
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "queries")
        files = glob.glob(os.path.join(root, "*.sql"))
        self.assertTrue(files)
        for f in files:
            text = open(f, encoding="utf-8").read()
            out = u.normalize_sql(text)
            last = [ln for ln in u.strip_sql_line_comments(text).splitlines() if ln.strip()][-1].split()[-1]
            self.assertTrue(out.endswith(last), f)
            self.assertEqual(out.count("?"), u.strip_sql_line_comments(text).count("?"), f)

    def test_evaluate_docs_bindings(self):
        """Запрос проверки экспертизы: три плейсхолдера (media, file storage, id) и WHERE в конце."""
        u = utils()
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "queries")
        out = u.normalize_sql(open(os.path.join(root, "evaluate_docs_script.sql"), encoding="utf-8").read())
        self.assertTrue(out.endswith("WHERE e.id = ?;"))
        self.assertEqual(out.count("?"), 3)


if __name__ == "__main__":
    unittest.main()

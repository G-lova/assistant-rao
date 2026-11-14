"""Чистка 14.11.2026: ресурсы по пути от корня репозитория, общие методы ЕИС в одной примеси.

* ``configs.resources.read_text`` читает промпты и SQL из любого текущего каталога, в UTF-8, с кэшем;
* ``load_sql_query`` не зависит от текущего каталога;
* ``CloudStorageParser`` и ``EISParser`` берут методы SOAP ЕИС из ``EISSoapMixin`` (а не из двух копий).
"""
import os
import tempfile
import unittest


class ResourcesTest(unittest.TestCase):
    """``configs.resources``."""

    def test_any_cwd(self):
        """Из чужого каталога промпт читается; повторное чтение — из кэша; русский текст — UTF-8."""
        from configs import resources

        cwd = os.getcwd()
        tmp = tempfile.mkdtemp()
        try:
            os.chdir(tmp)
            text = resources.read_text("prompts/ocr_prompt.txt")
            self.assertIn("изображени", text.lower())
            before = resources._read_cached.cache_info().hits
            resources.read_text("prompts/ocr_prompt.txt")
            self.assertEqual(resources._read_cached.cache_info().hits, before + 1)
            self.assertTrue(resources.resource_path("xml").is_dir())
        finally:
            os.chdir(cwd)
            os.rmdir(tmp)

    def test_absolute_path(self):
        """Абсолютный путь — как есть."""
        from configs import resources

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("Проверка")
        try:
            self.assertEqual(resources.read_text(f.name), "Проверка")
        finally:
            os.remove(f.name)

    def test_sql_any_cwd(self):
        """``load_sql_query`` из чужого каталога: запрос в одну строку."""
        from tests.stubs import stubbed_import

        cwd = os.getcwd()
        tmp = tempfile.mkdtemp()
        try:
            with stubbed_import("configs.utils") as utils:
                os.chdir(tmp)
                sql = utils.load_sql_query("evaluate_docs_script.sql")
            self.assertTrue(sql.strip())
            self.assertNotIn("\n", sql)
        finally:
            os.chdir(cwd)
            os.rmdir(tmp)


class EisMixinTest(unittest.TestCase):
    """Общие методы ЕИС — в ``EISSoapMixin``."""

    def test_shared(self):
        from tests.stubs import stubbed_import

        with stubbed_import("configs.parsing") as p:
            from configs import eis_common, eis_parsing

            for cls in (p.CloudStorageParser, eis_parsing.EISParser):
                self.assertTrue(issubclass(cls, eis_common.EISSoapMixin), cls)
                for name in ("_download_eis_archive", "_process_eis_archive", "_extract_eis_archive_url",
                             "_extract_eis_error_info", "_sanitize_filename"):
                    self.assertNotIn(name, cls.__dict__, f"{cls.__name__}.{name} — копия вместо общего метода")
            mixin = eis_common.EISSoapMixin()
            self.assertTrue(mixin._sanitize_filename("a/b:c?.pdf").endswith(".pdf"))
            self.assertTrue(mixin._sanitize_filename("???").startswith(("file_", "_")))


if __name__ == "__main__":
    unittest.main()

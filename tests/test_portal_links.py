"""Ссылки на портал экспертиз: файл (``/storage``, ``/api/media``) или страница интерфейса (``/expertises/edit``).

20.11.2026: ссылка https://stage.rao0123.1t.ws/expertises/edit (страница редактирования экспертизы) уходила в
``download_media`` → «Ошибка скачивания media …» с повторами; файла по такой ссылке не получить.
"""
import asyncio
import unittest
from unittest import mock


def parsing():
    """``configs.parsing`` с заглушками пакетов, которых может не быть (aiofiles и др.)."""
    from tests.stubs import stubbed_import

    with stubbed_import("configs.parsing") as module:
        return module


class PortalLinksTest(unittest.TestCase):
    def parser(self):
        p = parsing().CloudStorageParser(http_manager=None, expertise_object=None)
        p.download_media = mock.AsyncMock(return_value={"status": "success"})
        # маппинг доменов строится в __init__ со ссылкой на прежний метод — обновляем на подмену
        for host, fn in list(p.supported_domains.items()):
            if getattr(fn, "__name__", "") == "download_media":
                p.supported_domains[host] = p.download_media
        return p

    def test_is_portal_file_url(self):
        is_portal_file_url = parsing().is_portal_file_url
        self.assertTrue(is_portal_file_url("https://expert.rusacademedu.ru/storage/123/ТЗ.docx"))
        self.assertTrue(is_portal_file_url("https://stage.rao0123.1t.ws/api/media/77/a.pdf"))
        self.assertFalse(is_portal_file_url("https://stage.rao0123.1t.ws/expertises/edit"))
        self.assertFalse(is_portal_file_url("https://expert.rusacademedu.ru/experts/5"))

    def test_page_skipped_without_request(self):
        p = self.parser()
        res = asyncio.run(p.parse_cloud_link("https://stage.rao0123.1t.ws/expertises/edit", "1"))
        self.assertEqual(res["status"], "error")
        self.assertTrue(res["skipped"])
        self.assertIn("страницу портала", res["error"])
        p.download_media.assert_not_called()

    def test_files_go_to_media(self):
        p = self.parser()
        for url in ("https://expert.rusacademedu.ru/storage/123/ТЗ.docx",
                    "https://stage.rao0123.1t.ws/api/media/77/a.pdf"):
            self.assertEqual(asyncio.run(p.parse_cloud_link(url, "1"))["status"], "success")
        self.assertEqual(p.download_media.await_count, 2)


if __name__ == "__main__":
    unittest.main()

"""Большие XML ЕИС (больше ``MAX_XML_BYTES``): облегчённый разбор ``parse_xml_lite`` и предел ``XML_LITE_MAX_BYTES``.

17.11.2026 извещение epNotificationEF2020 больше 30 МБ не разбиралось совсем (ValueError с трассировкой на каждой
попытке); полный разбор двух версий по 40 МБ — около 730 МБ памяти.
"""
import asyncio
import io
import types
import unittest
import zipfile
from unittest import mock

from risk_monitoring import xml_events


def notification(n_items: int, price: str = "100.00", extra: str = "") -> bytes:
    """Извещение с ``n_items`` позициями (по 3 характеристики) и двумя вложениями."""
    items = "".join(
        f"<purchaseObject><name>Позиция {i}</name><price>{price}</price>"
        + "".join(f"<characteristic><name>Х{j}</name><value>{i}-{j}</value></characteristic>" for j in range(3))
        + "</purchaseObject>" for i in range(n_items))
    return ("<epNotificationEF2020 xmlns='http://zakupki.gov.ru/oos/EPtypes/1'><versionNumber>2</versionNumber>"
            "<commonInfo><purchaseNumber>0161100002926000011</purchaseNumber></commonInfo>"
            f"<purchaseObjects>{items}</purchaseObjects>{extra}"
            "<attachmentsInfo>"
            "<attachmentInfo><fileName>ТЗ.docx</fileName><url>https://zakupki.gov.ru/a1</url></attachmentInfo>"
            "<attachmentInfo><fileName>Проект контракта.docx</fileName><url>https://zakupki.gov.ru/a2</url></attachmentInfo>"
            "</attachmentsInfo></epNotificationEF2020>").encode()


class LiteParseTest(unittest.TestCase):
    """``parse_xml_lite``: повторы урезаются, вложения и сведения остаются."""

    def parse(self, raw, **limits):
        from configs import settings

        with mock.patch.multiple(settings, **{"XML_LITE_MAX_CHILDREN": 5, "XML_LITE_MAX_TEXT": 100, **limits}):
            return xml_events.parse_xml_lite(raw)

    def test_items_capped_attachments_kept(self):
        tag, version, body = self.parse(notification(50))
        self.assertEqual(tag, "epNotificationEF2020")
        self.assertEqual(version, 2)
        items = body["purchaseObjects"]["purchaseObject"]
        self.assertEqual(len(items), 5)                                  # 45 позиций отброшено
        self.assertEqual(len(items[0]["characteristic"]), 3)             # внутри позиции — без изменений
        self.assertEqual(body["_lite"]["dropped"], 45)
        self.assertEqual(len(xml_events.attachments(body)), 2)           # вложения на месте
        self.assertEqual(xml_events.extract_facts(tag, body).get("purchase_number"), "0161100002926000011")

    def test_long_text_cut(self):
        _, _, body = self.parse(notification(1, extra="<note>" + "А" * 500 + "</note>"))
        self.assertEqual(len(body["note"]), 100)
        self.assertEqual(body["_lite"]["texts_cut"], 1)

    def test_hard_limit_and_dtd(self):
        with self.assertRaises(xml_events.XmlTooLarge):
            self.parse(notification(50), XML_LITE_MAX_BYTES=1000)
        with self.assertRaises(ValueError):
            self.parse(b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>')

    def test_lite_marker_not_a_change(self):
        """Отметка ``_lite`` — служебная: в сравнении версий не участвует."""
        self.assertIn("_lite", xml_events.IGNORED_KEYS)


class AnalyzerLiteTest(unittest.TestCase):
    """``XmlEventAnalyzer``: большой XML — облегчённый разбор без сравнения полей; больше предела — без трассировки."""

    def analyzer(self, raw_by_id):
        from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

        a = XmlEventAnalyzer.__new__(XmlEventAnalyzer)
        cache = types.SimpleNamespace(parsed={})
        a._cache = lambda: cache

        async def read(url, name):
            return raw_by_id[url]

        a.archives = types.SimpleNamespace(read=read)
        return a

    def test_big_xml_parsed_lite(self):
        from configs import settings

        big = notification(50)
        a = self.analyzer({"u1": big})
        with mock.patch.object(xml_events, "MAX_XML_BYTES", 3000), \
                mock.patch.multiple(settings, XML_LITE_MAX_CHILDREN=5, XML_LITE_MAX_TEXT=1000):
            tag, _, body, size = asyncio.run(a._parse({"id": 1, "archive_url": "u1", "entry_name": "n.xml"}))
        self.assertEqual(size, len(big))
        self.assertEqual(body["_lite"]["dropped"], 45)

    def test_too_large_entry_no_traceback(self):
        """Запись архива больше ``XML_LITE_MAX_BYTES`` — ``XmlTooLarge``; в журнале предупреждение без трассировки."""
        from configs import settings
        from risk_monitoring.xml_event_analyzer import ArchiveCache, XmlEventAnalyzer

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("n.xml", notification(50))
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf, \
                mock.patch.object(settings, "XML_LITE_MAX_BYTES", 1000):
            with self.assertRaises(xml_events.XmlTooLarge):
                ArchiveCache._read_member(zf, "n.xml", "n.xml")

        a = XmlEventAnalyzer.__new__(XmlEventAnalyzer)
        a.push_results = False
        with self.assertLogs("risk_monitoring.xml_event_analyzer", level="WARNING") as logs:
            out = asyncio.run(a._failure(7, 1, xml_events.XmlTooLarge("n.xml: XML 250 МБ больше предела 200 МБ")))
        self.assertEqual(out["status"], "error")
        self.assertTrue(all("Traceback" not in m and "ERROR" not in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()

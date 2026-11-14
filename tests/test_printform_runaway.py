"""Печатная форма, не похожая на извещение (электронный контракт): значения меток не «убегают» в остаток документа.

Файл 29759 (stage, 13.11.2026): «Наименование объекта закупки» заняло 531 тыс. символов, ai_analysis — 2,1 млн,
API внешней БД ответил HTTP 413. Проверяется:

* ``PrintForm``: значение метки — не больше ``MAX_VALUE_LINES`` строк и ``MAX_VALUE_CHARS`` символов;
* ``printform_passport.build``: название и объект закупки в пределах, ``summary`` короткий;
* ``is_runaway``: старый раздутый результат распознаётся (пересборка вместо дозаписи), новый — нет;
* PATCH ai_analysis: тело в UTF-8, тело больше предела не отправляется, ошибка называет крупные части.
"""
import asyncio
import json
import unittest
from unittest import mock


def contract_print_form(lines: int = 5000) -> str:
    """Текст «печатной формы электронного контракта»: метка извещения, а дальше — длинный текст без меток."""
    head = ["Печатная форма электронного контракта", "Номер извещения", "0372100037426000405",
            "Наименование объекта закупки", "Поставка медицинских изделий"]
    body = [f"{i}. Поставщик обязуется поставить товар в соответствии со спецификацией, пункт {i}." for i in range(lines)]
    return "\n".join(head + body)


class PrintFormValueLimitTest(unittest.TestCase):
    """``knowledge_store.eis_printform.PrintForm``."""

    def test_value_limited(self):
        from knowledge_store import eis_printform as ep

        form = ep.PrintForm(contract_print_form())
        value = form.value("object_name")
        self.assertTrue(value.startswith("Поставка медицинских изделий"))
        self.assertLessEqual(len(form.pairs["object_name"]), ep.MAX_VALUE_LINES)
        self.assertLessEqual(len(value), ep.MAX_VALUE_CHARS)
        self.assertEqual(form.value("number"), "0372100037426000405")

    def test_notice_values_unchanged(self):
        """Обычные короткие значения извещения не меняются."""
        from knowledge_store import eis_printform as ep

        text = "\n".join(["Извещение о проведении электронного аукциона", "Номер извещения", "0123",
                          "Наименование объекта закупки", "Поставка бумаги", "для офиса",
                          "Способ определения поставщика", "Электронный аукцион"])
        form = ep.PrintForm(text)
        self.assertEqual(form.value("object_name"), "Поставка бумаги для офиса")
        self.assertEqual(form.value("method"), "Электронный аукцион")


class PassportTest(unittest.TestCase):
    """``risk_monitoring.printform_passport``."""

    def test_build_and_runaway(self):
        from risk_monitoring import printform_passport as pp

        extraction, profile, _ = pp.build(contract_print_form(), {"type_decode": "Контракт", "group": "contract"}, {})
        raw = extraction["raw_data"]
        self.assertLessEqual(len(raw["procurement_subject"]["description"]), pp.SUBJECT_MAX_CHARS)
        self.assertLessEqual(len(raw["document_name"]["value"] or ""), pp.TITLE_MAX_CHARS)
        self.assertLess(len(json.dumps(extraction, ensure_ascii=False)), 20000)
        self.assertFalse(pp.is_runaway({"raw_data": raw}))
        # результат, собранный до правки: объект закупки — весь остаток документа
        old = {"raw_data": {**raw, "procurement_subject": {"description": "x" * 531000}, "summary": "x" * 531000}}
        self.assertTrue(pp.is_runaway(old))
        self.assertFalse(pp.is_runaway({"raw_data": {"source": "llm", "summary": "x" * 531000}}))
        self.assertFalse(pp.is_runaway(None))


class PatchBodyTest(unittest.TestCase):
    """``RiskMonitoringAPI._patch_body``: UTF-8 и предел размера."""

    def api(self):
        """Клиент без конструктора; ``_request`` запоминает аргументы."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.risk_monitoring_api") as m:   # aiohttp может не быть — заглушка
            pass
        api = m.RiskMonitoringAPI.__new__(m.RiskMonitoringAPI)
        api.headers = {"Authorization": "Bearer t"}
        api.sent = []

        async def request(method, url=None, **kw):
            api.sent.append((method, kw))
            return {"status": "ok", "data": {"id": 1}}

        api._request = request
        return m, api

    def test_utf8_body(self):
        """Кириллица — UTF-8 (2 байта на букву), а не \\uXXXX; Content-Type и авторизация переданы."""
        m, api = self.api()
        asyncio.run(api.patch_ai_analysis("risk_monitoring_files", 7, {"resume": "Поставка"}))
        method, kw = api.sent[0]
        self.assertEqual(method, "PATCH")
        self.assertIn("Поставка".encode("utf-8"), kw["data"])
        self.assertNotIn(b"\\u", kw["data"])
        self.assertEqual(json.loads(kw["data"])["ai_analysis"]["resume"], "Поставка")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer t")
        self.assertIn("application/json", kw["headers"]["Content-Type"])

    def test_too_large_not_sent(self):
        """Тело больше предела — 413 без запроса, в ошибке крупные части."""
        m, api = self.api()
        from configs import settings

        with mock.patch.object(settings, "RM_PUSH_MAX_BYTES", 1000, create=True):
            with self.assertRaises(m.RiskAPIError) as ctx:
                asyncio.run(api.patch_ai_analysis("risk_monitoring_files", 7, {"raw_data": "я" * 2000, "status": "ok"}))
        self.assertEqual(ctx.exception.status, 413)
        self.assertIn("raw_data", str(ctx.exception))
        self.assertEqual(api.sent, [])


if __name__ == "__main__":
    unittest.main()

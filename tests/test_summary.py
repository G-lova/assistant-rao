"""Тесты сводного ЭЗ (этап 5): сборка data/trace, валидатор, блоки III–IV, отправка черновика."""
import asyncio
import json
import os
import re
import unittest
from pathlib import Path
import sys
import types
from unittest import mock

# В окружении без aiohttp/requests подставляем заглушки: ExternalAPIService импортируется лениво
try:
    import aiohttp  # noqa: F401
    import requests  # noqa: F401
except ImportError:
    for _name in ("aiohttp", "requests"):
        _stub = types.ModuleType(_name)
        _stub.ClientError = type("ClientError", (Exception,), {})
        sys.modules[_name] = _stub
    _hcm = types.ModuleType("configs.http_client_manager")
    _hcm.HTTPClientManager = object
    sys.modules["configs.http_client_manager"] = _hcm

from knowledge_store import export, facts, repository as repo, summary
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def field(key, kind, label="", ordinal=0):
    """Поле формы для тестов."""
    return {"field_key": key, "value_kind": kind, "label": label or key, "ordinal": ordinal}


FIELDS = [
    field("field1_1", "number"), field("field1_2", "number"),
    field("field2_1_1", "presence", "1.1. Наличие наименования"),
    field("field2_1_2", "presence", "1.2. Наличие адреса"),
    field("field2_2_4", "section"),
    field("field2_2_4_1", "compliance", "2.2.4.1. Соответствие сроков"),
    field("field2_2_4_2", "compliance", "2.2.4.2. Соответствие цены"),
    field("field2_2_4_text", "text"),
    field("field3", "text"), field("field4", "text"), field("rao_comment", "text"),
]


def fact(key, value, verified=True, quote=None, source="fact_extractor", doc=1, comment=None):
    """Строка pe_facts для тестов."""
    return {"fact_key": key, "value": {"value": value, "verified": verified, "comment": comment},
            "document_id": doc, "page": 2, "quote": quote, "confidence": 0.8, "source": source}


DOCS = {1: {"filename": "n.docx", "doc_code": "docIzvejenieFiles", "text": "Срок оплаты: 30 дней. Адрес: Москва"}}


class AssembleTests(unittest.TestCase):
    """assemble / validate без БД и LLM."""

    def test_keys_exactly_form_and_unproven_left_null(self):
        """data содержит ровно ключи формы; значения без доказательства ставятся как proposed."""
        rows = [
            fact("field2_1_1", 1, source="eis_xml", quote="fullName = Вуз"),         # XML — доказано
            fact("field2_1_2", 1, quote="Адрес: Москва"),                          # цитата есть в тексте
            fact("field2_2_4_1", 1, quote="цитаты нет в документе"),                # цитата выдумана → proposed
            fact("field2_2_4_2", 0, comment="цена не указана"),                     # «0» без цитаты допустим
        ]
        data, trace = summary.assemble(FIELDS, rows, DOCS, {"nmck": 1500000, "advance": None})
        self.assertEqual(set(data), {f["field_key"] for f in FIELDS})
        self.assertEqual((data["field2_1_1"], data["field2_1_2"]), (1, 1))
        self.assertEqual(data["field2_2_4_1"], 1)
        self.assertEqual(trace["field2_2_4_1"]["status"], "proposed")
        self.assertEqual(data["field2_2_4_2"], 0)
        self.assertEqual(data["field2_2_4"], 0)                      # итог подраздела: есть «0»
        self.assertEqual(data["field2_2_4_text"], "1. 2.2 цена не указана")   # пояснение к отрицательному результату
        self.assertEqual(data["field1_1"], 1500000.0)                  # из паспорта
        self.assertIsNone(data["field1_2"])
        self.assertEqual(summary.validate(data, FIELDS), [])

    def test_unproven_values_are_proposed_and_three_is_left_to_expert(self):
        """«1»/«0»/«2» без доказательства ставятся как proposed (с исходной цитатой модели); «3» остаётся эксперту."""
        rows = [fact("field2_1_1", 1, quote=None, verified=False), fact("field2_1_2", 1, verified=False, quote="Адрес: Москва"),
                fact("field2_2_4_1", 0, verified=False, comment="сведений нет"),
                fact("field2_2_4_2", 2, verified=False, comment="не предусмотрено")]
        rows[0]["value"]["model_quote"] = "Срок оплаты"
        data, trace = summary.assemble(FIELDS, rows, DOCS)
        self.assertEqual((data["field2_1_1"], data["field2_1_2"]), (1, 1))
        self.assertEqual(trace["field2_1_1"]["status"], "proposed")
        self.assertEqual(trace["field2_1_1"]["model_quote"], "Срок оплаты")
        self.assertEqual((data["field2_2_4_1"], data["field2_2_4_2"]), (0, 2))
        self.assertEqual((trace["field2_2_4_1"]["status"], trace["field2_2_4_2"]["status"]), ("proposed", "proposed"))
        three = fact("field2_1_1", 3, verified=False, comment="не по теме")
        data, trace = summary.assemble(FIELDS, [three], DOCS)
        self.assertIsNone(data["field2_1_1"])
        self.assertIn("note", trace["field2_1_1"])

    def test_general_info_from_xml_passport_and_documents(self):
        """name/inn/НМЦК/аванс — из фактов XML, состав комплекта — из документов; шифр и финансирование остаются эксперту."""
        fields = [field("code", "meta"), field("name", "meta"), field("inn", "meta"), field("documents", "meta"),
                  field("field1_1", "number"), field("field1_2", "number"), field("field1_3", "text"),
                  field("field1_2_unit", "text"),
                  field("f8", "presence", "1.8. Наличие информации об идентификационном коде закупки"),
                  field("f11", "presence", "1.11. Наличие информации о наименовании объекта закупки"),
                  field("f18", "presence", "1.18. Наличие информации о начальной (максимальной) цене"),
                  field("f22", "presence", "1.22. Наличие информации о размере аванса")]
        rows = [fact("f8", 1, source="eis_xml", quote="notificationInfo/contractConditionsInfo/IKZInfo/purchaseCode = 2325"),
                fact("f11", 1, source="eis_xml", quote="commonInfo/purchaseObjectInfo = Услуги связи"),
                fact("f18", 1, source="eis_xml", quote="notificationInfo/contractConditionsInfo/maxPriceInfo/maxPrice = 1500000.50"),
                fact("f22", 1, source="eis_xml", quote="notificationInfo/contractConditionsInfo/advancePaymentSum/sumInPercents = 30")]
        docs = {1: {"filename": "n.xml", "doc_code": "docIzvejenieFiles", "text": "т"},
                2: {"filename": "o.docx", "doc_code": "docOpisanieFiles", "text": "т"}}
        data, trace = summary.assemble(fields, rows, docs)
        self.assertEqual((data["name"], data["inn"], data["field1_1"], data["field1_2"], data["field1_2_unit"]),
                         ("Услуги связи", "2325", 1500000.5, 30.0, "%"))
        self.assertIn("n.xml", data["documents"])
        self.assertIn("o.docx", data["documents"])
        self.assertIsNone(data["code"])
        self.assertIsNone(data["field1_3"])

    def test_general_info_fallback_to_extraction_is_proposed(self):
        """Без XML предмет закупки и НМЦК берутся из данных извлечения документов и помечаются proposed."""
        fields = [field("name", "meta"), field("field1_1", "number")]
        extraction = {"raw_data": {"procurement_subject": {"description": "Оказание услуг связи"},
                                   "finances": [{"value": "1 200 000,00", "context": "НМЦК", "evidence": {"fragment": "НМЦК 1 200 000,00"}}]}}
        docs = {1: {"filename": "n.docx", "doc_code": "docIzvejenieFiles", "text": "т", "extraction": json.dumps(extraction)}}
        data, trace = summary.assemble(fields, [], docs)
        self.assertEqual((data["name"], data["field1_1"]), ("Оказание услуг связи", 1200000.0))
        self.assertEqual((trace["name"]["status"], trace["field1_1"]["status"]), ("proposed", "proposed"))

    def test_section_rules(self):
        """Итог подраздела: все решены → 1; есть нерешённый без «0» → None."""
        self.assertEqual(summary.section_result([1, 2]), 1)
        self.assertIsNone(summary.section_result([1, None]))
        self.assertEqual(summary.section_result([None, 0]), 0)
        self.assertIsNone(summary.section_result([]))

    def test_validate_catches_foreign_and_bad_values(self):
        """Валидатор находит чужой ключ, недопустимое значение и пропавший ключ."""
        data = {f["field_key"]: None for f in FIELDS}
        data["foreign"] = 1
        data["field2_1_1"] = 5
        del data["field4"]
        problems = summary.validate(data, FIELDS)
        self.assertTrue(any("foreign" in p for p in problems))
        self.assertTrue(any("field2_1_1" in p for p in problems))
        self.assertTrue(any("field4" in p for p in problems))

    def test_normalize_result(self):
        """Значения 1/0/2 приходят и числом, и строкой."""
        self.assertEqual([summary.normalize_result(v) for v in (1, "0", "2", True, "x", 7, None)],
                         [1, 0, 2, 1, None, None, None])


class BlocksTests(unittest.TestCase):
    """Блоки III–IV."""

    def setUp(self):
        """Данные с одним замечанием."""
        rows = [fact("field2_1_1", 1, source="eis_xml"), fact("field2_2_4_1", 0, comment="срок не указан")]
        self.data, self.trace = summary.assemble(FIELDS, rows, DOCS)

    def test_llm_sees_only_verified_remarks(self):
        """LLM получает только замечания (значение 0) и счётчики; нерешённые критерии не передаются."""
        seen = []

        async def llm(messages, schema):
            """Запоминает вход и отвечает текстом."""
            seen.append(messages[1]["content"])
            return json.dumps({"text": "Выявлено замечание по срокам."})

        stats = asyncio.run(summary.write_blocks(FIELDS, self.data, self.trace, llm))
        self.assertEqual(stats, {"checked": 2, "remarks": 1})
        self.assertEqual(self.data["field3"], "Выявлено замечание по срокам.")
        self.assertEqual(self.trace["field3"]["method"], "llm")
        self.assertIn("срок не указан", seen[0])
        self.assertNotIn("field2_1_2", seen[0])

    def test_no_remarks_means_no_llm_call(self):
        """Без замечаний LLM не вызывается, ставится шаблон."""
        rows = [fact("field2_1_1", 1, source="eis_xml")]
        data, trace = summary.assemble(FIELDS, rows, DOCS)

        async def llm(messages, schema):
            """Не должна вызываться."""
            raise AssertionError("LLM не нужна")

        asyncio.run(summary.write_blocks(FIELDS, data, trace, llm))
        self.assertIn("соответствует требованиям", data["field3"])
        self.assertEqual(trace["field4"]["method"], "template")

    def test_llm_failure_falls_back_to_template(self):
        """Сбой модели и мусорный ответ заменяются шаблоном с числом замечаний."""
        async def broken(messages, schema):
            """Возвращает не-JSON."""
            return "не json"

        asyncio.run(summary.write_blocks(FIELDS, self.data, self.trace, broken))
        self.assertEqual(self.trace["field4"]["method"], "template")
        self.assertIn("за исключением указанных несоответствий", self.data["field4"])


class FakeResponse:
    """Ответ aiohttp для проверки отправки."""

    def __init__(self, status, body):
        self.status, self._body = status, body
        self.headers = {"content-type": "application/json"}

    async def json(self, content_type=None):
        """Тело ответа."""
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Сессия, запоминающая запрос."""

    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body or {}

    def post(self, url, headers=None, json=None):
        """Фиксирует вызов и возвращает ответ."""
        self.calls.append((url, headers, json))
        return FakeResponse(self.status, self.body)


class FakeManager:
    """HTTPClientManager-заглушка."""

    def __init__(self, session):
        self.session = session

    def get_session(self):
        """Возвращает сессию."""
        return self.session


class ExportTests(unittest.TestCase):
    """Отправка черновика через ExternalAPIService (Config.get_external_api_config)."""

    def test_uses_external_api_service_and_payload(self):
        """URL и заголовки из get_external_api_config; тело {id, data}."""
        session = FakeSession()
        cfg = {"url": "https://x/api/expertise/set-hint-for-rao-expert", "headers": {"X-API-Key": "k"}}
        with mock.patch("conclusion.external_api_service.Config.get_external_api_config", return_value=cfg) as m:
            res = asyncio.run(export.send_draft(FakeManager(session), 5, {"field3": "т"}, "stage"))
        m.assert_called_once_with("stage")
        self.assertEqual(res, {"ok": True})
        self.assertEqual(session.calls[0], (cfg["url"], cfg["headers"], {"id": 5, "data": {"field3": "т"}}))

    def test_http_error_and_exception_are_returned_not_raised(self):
        """Ошибка HTTP и сбой сети возвращаются в результате (заключение не теряется), ключ в ответ не попадает."""
        cfg = {"url": "https://x", "headers": {"X-API-Key": "secret"}}
        with mock.patch("conclusion.external_api_service.Config.get_external_api_config", return_value=cfg):
            res = asyncio.run(export.send_draft(FakeManager(FakeSession(422, {"message": "bad"})), 5, {}))
            self.assertFalse(res["ok"])
            self.assertIn("422", res["error"])
            self.assertIn("bad", res["error"])

            class Broken:
                """Менеджер без сессии."""

                def get_session(self):
                    raise ConnectionError("down")

            res = asyncio.run(export.send_draft(Broken(), 5, {}))
        self.assertFalse(res["ok"])
        self.assertNotIn("secret", json.dumps(res))


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class SummaryDbTests(unittest.TestCase):
    """precheck и generate_summary на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт схему pe_* (миграции 001 и 002, vector → real[])."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        sql = (MIGRATIONS / "001_knowledge_store.sql").read_text(encoding="utf-8")
        sql = re.sub(r"CREATE EXTENSION[^\n]*\n", "", sql)
        sql = re.sub(r"[^\n]*USING hnsw[^\n]*\n", "", sql).replace("vector(1024)", "real[]")
        cls.conn._run("DROP TABLE IF EXISTS pe_summary_opinions, pe_facts, pe_chunks, pe_documents, pe_form_fields, pe_procurements CASCADE;")
        cls.conn._run(sql)
        cls.conn._run((MIGRATIONS / "002_form_fields_derived.sql").read_text(encoding="utf-8"))

    def run_async(self, coro):
        """Запускает корутину."""
        return asyncio.run(coro)

    def test_409_cases_and_generation(self):
        """Нет данных / очищенные тексты → 409; неподдерживаемая форма → 422; сборка сохраняет data и trace."""
        form = "44fz_competition_obj6"
        c = self.conn
        c._run(f"DELETE FROM pe_form_fields WHERE form_code = '{form}'")
        for key, n, label, kind in [("k_name", 1, "1.1. Наличие наименования", "presence"),
                                    ("k_cmp", 2, "2.1.1. Соответствие наименований", "compliance"),
                                    ("field3", 3, "Вывод", "text")]:
            c._run(f"INSERT INTO pe_form_fields (form_code, field_key, ordinal, label, value_kind) "
                   f"VALUES ('{form}', '{key}', {n}, '{label}', '{kind}')")
        # 1) документов нет
        with self.assertRaises(summary.NoDataError):
            self.run_async(summary.precheck(c, 8101))
        # 2) тексты очищены
        c._run("INSERT INTO pe_procurements (expertise_id, law, method, object_code, check_type2) VALUES (8101, '44-ФЗ', 'Конкурс', 6, 1)")
        c._run("INSERT INTO pe_documents (expertise_id, doc_code, filename, sha256, text_full, text_purged_at) "
               "VALUES (8101, 'docIzvejenieFiles', 'n.docx', 'h1', NULL, NOW())")
        with self.assertRaises(summary.TextPurgedError):
            self.run_async(summary.precheck(c, 8101))
        # 3) тексты есть, форма не поддерживается (223-ФЗ)
        c._run("UPDATE pe_documents SET text_full = 'Адрес: Москва', text_purged_at = NULL WHERE expertise_id = 8101")
        c._run("UPDATE pe_procurements SET law = '223-ФЗ' WHERE expertise_id = 8101")
        with self.assertRaises(summary.UnsupportedFormError) as ctx:
            self.run_async(summary.precheck(c, 8101))
        self.assertEqual(ctx.exception.http_status, 422)
        c._run("UPDATE pe_procurements SET law = '44-ФЗ' WHERE expertise_id = 8101")
        # 4) фактов нет и построить нечем
        with self.assertRaises(summary.NoFactsError):
            self.run_async(summary.generate_summary(c, 8101, None))
        # 5) факты строятся колбэком, сборка сохраняет data и trace
        doc_id = c._run("SELECT id FROM pe_documents WHERE expertise_id = 8101").strip()

        async def build_facts():
            """Колбэк вставляет два факта."""
            c._run(f"INSERT INTO pe_facts (expertise_id, fact_key, value, document_id, page, quote, confidence, source) VALUES "
                   f"(8101, 'k_name', '{{\"value\": 1, \"verified\": true, \"comment\": null}}', {doc_id}, 1, 'Адрес: Москва', 0.8, 'fact_extractor'), "
                   f"(8101, 'k_cmp', '{{\"value\": 0, \"verified\": true, \"comment\": \"не совпадает\"}}', {doc_id}, 1, NULL, 0.8, 'fact_extractor')")

        async def llm(messages, schema):
            """Ответ модели для блока."""
            return json.dumps({"text": "Вывод по замечанию."})

        res = self.run_async(summary.generate_summary(c, 8101, llm, build_facts))
        self.assertEqual(res["form_code"], form)
        self.assertEqual(res["status"], "draft")
        self.assertEqual(res["data"], {"k_name": 1, "k_cmp": 0, "field3": "Вывод по замечанию."})
        self.assertEqual(c._run("SELECT status FROM pe_summary_opinions WHERE expertise_id = 8101"), "draft")
        stored = json.loads(c._run("SELECT data FROM pe_summary_opinions WHERE expertise_id = 8101"))
        self.assertEqual(set(stored), {"k_name", "k_cmp", "field3"})
        trace = json.loads(c._run("SELECT trace FROM pe_summary_opinions WHERE expertise_id = 8101"))
        self.assertEqual(trace["k_name"]["quote"], "Адрес: Москва")
        c._run(f"DELETE FROM pe_form_fields WHERE form_code = '{form}'")


if __name__ == "__main__":
    unittest.main()


class NoticeTextFallbackTests(unittest.TestCase):
    """Наименование и идентификационный код из текста печатной формы извещения."""

    def test_name_and_ikz_from_notice_text(self):
        """Без XML и extraction наименование и ИКЗ берутся регулярками из текста извещения (proposed)."""
        fields = [field("name", "meta"), field("inn", "meta")]
        text = "Наименование объекта закупки: Услуги по организации отдыха\nИКЗ 262012345678901234567890123456789012"
        docs = {1: {"filename": "n.html", "doc_code": "docIzvejenieFiles", "text": text}}
        data, trace = summary.assemble(fields, [], docs)
        self.assertEqual(data["name"], "Услуги по организации отдыха")
        self.assertEqual(data["inn"], "262012345678901234567890123456789012")
        self.assertEqual(trace["name"]["status"], "proposed")


class NmckMethodRuleTests(unittest.TestCase):
    """Критерии про неприменённые методы расчёта НМЦК получают «2»."""

    def test_other_methods_not_applicable(self):
        """При методе рыночных цен критерии нормативного/затратного методов — 2, замечаний нет."""
        fields = [field("m_norm", "compliance", "2.2.7. Соответствие законодательству расчета НМЦК нормативным методом"),
                  field("m_cost", "compliance", "2.2.7. Соответствие законодательству расчета НМЦК затратным методом"),
                  field("m_proj", "compliance", "2.2.7. Соответствие законодательству расчета НМЦК проектно-сметным методом")]
        docs = {1: {"filename": "o.xlsx", "doc_code": "docObosnovanie",
                    "text": "Используемый метод определения НМЦК с обоснованием Метод сопоставления рыночных цен (анализ рынка)"}}
        rows = [fact("m_norm", 0, verified=False), fact("m_cost", 0, verified=False), fact("m_proj", 0, verified=False)]
        data, trace = summary.assemble(fields, rows, docs)
        self.assertEqual((data["m_norm"], data["m_cost"], data["m_proj"]), (2, 2, 2))
        self.assertEqual(trace["m_norm"]["status"], "derived")
        self.assertEqual(summary.collect_remarks(fields, data, trace), [])


class FallbackBlockTests(unittest.TestCase):
    """Шаблонный текст блока IV самостоятелен."""

    def test_field4_is_self_contained(self):
        """field4 содержит итог и суть замечаний и не отсылает к разделу «Вывод»."""
        remarks = [{"criterion": "1.14. Наличие информации о единице измерения", "comment": "", "quote": ""},
                   {"criterion": "2.2.7.2. Соответствие потенциальных поставщиков", "comment": "", "quote": ""}]
        text = summary.fallback_block("field4", remarks, {"checked": 60, "remarks": 2})
        self.assertNotIn("раздел", text.lower())
        self.assertIn("Заказчику рекомендуется:", text)


class ExpertStyleTextTests(unittest.TestCase):
    """Тексты в стиле экспертов: field3, блок 1, чистка ссылок на фрагменты."""

    def test_field3_fallback_lists_criteria(self):
        """field3: «Выявлены несоответствия и недостатки по критериям:» и строки «номер. название. суть»."""
        remarks = [{"criterion": "1.14. Наличие информации о единице измерения", "comment": "Единица измерения не указана.", "quote": ""}]
        text = summary.fallback_block("field3", remarks, {"checked": 5, "remarks": 1})
        remarks[0]["number"] = "2.2.7.2"
        remarks[0]["criterion"] = "2.2.7.2. Соответствие потенциальных поставщиков"
        text = summary.fallback_block("field3", remarks, {"checked": 5, "remarks": 1}, number="0373100100526000035")
        self.assertIn("Информация, представленная в извещении № 0373100100526000035, соответствует требованиям законодательства.", text)
        self.assertIn("Выявлены несоответствия и недостатки по критериям:", text)
        self.assertIn("2.2.7.2 «Соответствие потенциальных поставщиков». Единица измерения не указана.", text)

    def test_field4_without_remarks(self):
        """Без замечаний field4 — «целесообразно оформить документацию и осуществить закупку»."""
        text = summary.fallback_block("field4", [], {"checked": 5, "remarks": 0}, "услуги связи")
        self.assertIn("на услуги связи соответствуют требованиям", text)
        self.assertIn("целесообразно оформить документацию", text)

    def test_clean_comment_removes_fragment_refs(self):
        """«В фрагменте 6» заменяется ссылкой на документ."""
        self.assertEqual(summary.clean_comment("В фрагменте 6 указано X", "p.docx"), "В документе «p.docx» указано X")
        self.assertEqual(summary.clean_comment("Из фрагментов 1 и 3 видно", None), "Из документации видно")

    def test_section_one_text(self):
        """Текст блока 1: без замечаний — «соответствует», с замечаниями — нумерованный перечень с номерами критериев."""
        fields = [field("field2_1_text", "text"),
                  field("field2_1_1", "presence", "1.1. Наличие информации о заказчике"),
                  field("field2_1_2", "presence", "1.2. Наличие информации о почте")]
        data, _ = summary.assemble(fields, [fact("field2_1_1", 1, quote="Срок оплаты"), fact("field2_1_2", 1, quote="Адрес: Москва")], DOCS)
        self.assertIn("соответствует требованиям", data["field2_1_text"])
        rows = [fact("field2_1_1", 1, quote="Срок оплаты"), fact("field2_1_2", 0, comment="Почта не указана.")]
        data, _ = summary.assemble(fields, rows, DOCS)
        self.assertIn("необходимо обратить внимание", data["field2_1_text"])
        self.assertIn("1. 1.2 Почта не указана.", data["field2_1_text"])


class NmckTextFieldTests(unittest.TestCase):
    """Правило методов НМЦК не должно задевать текстовые поля `*_text`."""

    def test_text_field_untouched(self):
        """У ``*_text`` та же подпись, что у критерия, но значение остаётся текстом/None, а не «2»."""
        label = "2.2.7. Соответствие законодательству расчета НМЦК нормативным методом"
        fields = [field("m_norm", "compliance", label), field("m_norm_text", "text", label)]
        docs = {1: {"filename": "o.xlsx", "doc_code": "d", "text": "Используемый метод определения НМЦК Метод сопоставления рыночных цен"}}
        data, _ = summary.assemble(fields, [], docs)
        self.assertEqual(data["m_norm"], 2)
        self.assertIsNone(data["m_norm_text"])
        self.assertEqual(summary.validate(data, fields), [])


class Field4SampleTests(unittest.TestCase):
    """field4 по образцу «Пример ЭЗ (конкурс)»: способ, номер, предмет, рекомендации; шифр = реестровый номер."""

    def test_field4_like_sample(self):
        """Заключение: «Извещение и документация о проведении открытого конкурса в электронной форме для закупки № … на … соответствуют …»."""
        remarks = [{"number": "2.2", "criterion": "2.2. Соответствие обоснования НМЦК", "comment": "x", "quote": ""}]
        text = summary.fallback_block("field4", remarks, {"checked": 9, "remarks": 1}, "оказание услуг",
                                      "0373100100526000035", summary.procedure_phrase("44fz_competition_obj6"))
        self.assertTrue(text.startswith("Извещение и документация о проведении открытого конкурса в электронной форме "
                                        "для закупки № 0373100100526000035 на оказание услуг соответствуют требованиям законодательства, "
                                        "за исключением указанных несоответствий и недостатков."))
        self.assertIn("Заказчику рекомендуется:\n- учесть и устранить замечания в описании объекта закупки, обосновании НМЦК и проекте контракта (критерии 2.2).", text)

    def test_code_is_registry_number(self):
        """code берётся из реестрового номера в имени файла извещения."""
        fields = [field("code", "meta")]
        docs = {1: {"filename": "Печатная-форма-извещения-№-0301100027726000035-(версия-1).html", "doc_code": "docIzvejenieFiles", "text": "т"}}
        data, trace = summary.assemble(fields, [], docs)
        self.assertEqual(data["code"], "0301100027726000035")


class BlockLlmTests(unittest.IsolatedAsyncioTestCase):
    """LLM для блоков III–IV: повтор, сжатая нагрузка, причина отката на шаблон в trace."""

    async def test_retry_then_template_reason(self):
        """Две неудачи подряд → шаблон и trace.llm_error; успех со второй попытки → method=llm."""
        calls = []

        async def bad(messages, schema):
            calls.append(messages)
            return 'не json'

        fields = [field("f", "presence", "1.4. Наличие почты"), field("field4", "text")]
        data, trace = {"f": 0, "field4": None, "name": "услуги", "code": "0301100027726000035"}, {"f": {"comment": "нет", "document": "n.html"}}
        await summary.write_blocks(fields, data, trace, bad, "44fz_competition_obj6")
        self.assertEqual(len(calls), 2)
        self.assertEqual(trace["field4"]["method"], "template")
        self.assertIn("llm_error", trace["field4"])
        payload = json.loads(calls[0][1]["content"])
        self.assertEqual(payload["замечания"][0]["номер"], "1.4")
        self.assertEqual(payload["способ определения поставщика"], "открытого конкурса в электронной форме")

        answers = iter(['не json', '{"text": "Итог."}'])

        async def flaky(messages, schema):
            return next(answers)

        data["field4"] = None
        await summary.write_blocks(fields, data, trace, flaky, "44fz_competition_obj6")
        self.assertEqual((data["field4"], trace["field4"]["method"]), ("Итог.", "llm"))


class RenderBlockTests(unittest.TestCase):
    """Сборка текста блоков из структурированного ответа модели."""

    REM = [{"number": "2.2", "criterion": "2.2. Соответствие обоснования НМЦК", "comment": "x"}]

    def test_field4_sample1(self):
        """Мало замечаний: «…соответствуют…, за исключением… Заказчику рекомендуется: - …»."""
        parsed = {"areas": [], "note": "", "recommendations": ["указывать дату составления обоснования НМЦК"]}
        text = summary.render_block("field4", parsed, self.REM, "услуги", "0301100027726000035", "открытого конкурса в электронной форме")
        self.assertIn("соответствуют требованиям законодательства, за исключением указанных несоответствий и недостатков.", text)
        self.assertIn("Заказчику рекомендуется:\n- указывать дату составления обоснования НМЦК.", text)

    def test_field4_sample2_many_remarks(self):
        """Много замечаний: «…имеют недостатки… а именно: - область — суть… Следует отметить… Заказчику рекомендуется»."""
        parsed = {"areas": [{"area": "при расчёте НМЦК", "summary": "нет ценовых предложений"}], "note": "это не позволяет проверить обоснование",
                  "recommendations": ["прикладывать копии ценовых предложений"]}
        text = summary.render_block("field4", parsed, self.REM * 9, "услуги", "0373100004325000022", "открытого конкурса в электронной форме")
        self.assertTrue(text.startswith("Извещение о проведении открытого конкурса в электронной форме для закупки № 0373100004325000022 на услуги и электронные документы имеют недостатки"))
        self.assertIn("- при расчёте НМЦК — нет ценовых предложений", text)
        self.assertIn("Следует отметить, что это не позволяет проверить обоснование.", text)

    def test_field3(self):
        """field3: абзац об извещении и строки «номера. суть»."""
        parsed = {"notice": "не указан адрес электронной почты", "items": [{"numbers": "2.2.7.1", "text": "использованы данные двух поставщиков"}]}
        text = summary.render_block("field3", parsed, self.REM, None, "0301100027726000035", None)
        self.assertIn("за исключением: не указан адрес электронной почты.", text)
        self.assertIn("Выявлены несоответствия и недостатки по критериям:\n2.2.7.1. использованы данные двух поставщиков", text)

    def test_truncated_json_is_repaired_and_deduped(self):
        """Обрезанный ответ с повторами чинится, дубли убираются, пунктов не больше 6."""
        raw = '{"areas": [], "note": "", "recommendations": [' + ", ".join('"учесть замечания по НМЦК"' for _ in range(40)) + ', "хвост обре'
        parsed = summary.parse_block(raw)
        text = summary.render_block("field4", parsed, self.REM, None, None, None)
        self.assertEqual(text.count("- учесть замечания по НМЦК"), 1)

    def test_empty_answer_with_remarks_rejected(self):
        """Пустой ответ по field3 при наличии замечаний не должен давать «соответствует»: возвращается None."""
        self.assertIsNone(summary.render_block("field3", {"notice": "", "items": []}, self.REM, None, "1", None))
        text = summary.render_block("field3", {"notice": "", "items": []},
                                    [{"number": "1.4", "criterion": "1.4. Наличие информации о почте", "comment": "x"}], None, "1", None)
        self.assertIn("за исключением: Наличие информации о почте", text)
        mixed = [{"number": "1.4", "criterion": "1.4. Почта", "comment": "x"}, {"number": "2.2", "criterion": "2.2. НМЦК", "comment": "y"}]
        self.assertIsNone(summary.render_block("field3", {"notice": "", "items": []}, mixed, None, "1", None))   # пропущен раздел 2

    def test_runaway_output_falls_back(self):
        """Бесконечный/обрезанный ответ модели → шаблон с причиной в trace."""
        async def runaway(messages, schema):
            return "не json " + "а" * 9000

        fields = [field("f", "presence", "1.4. Наличие почты"), field("field4", "text")]
        data, trace = {"f": 0, "field4": None}, {"f": {"comment": "нет"}}
        asyncio.run(summary.write_blocks(fields, data, trace, runaway, "44fz_competition_obj6"))
        self.assertEqual(trace["field4"]["method"], "template")
        self.assertIn("llm_error", trace["field4"])


class DefragmentTests(unittest.TestCase):
    """В заключении не должно быть слова «фрагмент»."""

    def test_forms(self):
        """Все падежи заменяются на «документ…», регистр сохраняется."""
        self.assertEqual(summary.defragment("В предоставленных фрагментах отсутствует информация"), "В предоставленных документах отсутствует информация")
        self.assertEqual(summary.defragment("Фрагменты не содержат сведений; ни один из фрагментов"), "Документы не содержат сведений; ни один из документов")
        self.assertEqual(summary.defragment("на основании представленных фрагментов невозможно"), "на основании представленных документов невозможно")
        self.assertEqual(summary.defragment("Фрагмент 3 и фрагмента"), "Документ 3 и документа")

    def test_applied_everywhere(self):
        """Очищаются комментарии критериев, тексты *_text и блоки III–IV."""
        fields = [field("f", "presence", "1.4. Наличие почты"), field("f_text", "text", "1.4. Наличие почты"),
                  field("field4", "text")]
        rows = [fact("f", 0, verified=False, comment="В фрагментах отсутствует информация о почте.")]
        data, trace = summary.assemble(fields, rows, DOCS)
        self.assertNotIn("фрагмент", (data["f_text"] + trace["f"]["comment"]).lower())
        asyncio.run(summary.write_blocks(fields, data, trace, None, "44fz_competition_obj6"))
        self.assertNotIn("фрагмент", data["field4"].lower())
        three = fact("f", 3, verified=False, comment="Фрагменты не по теме.")
        _, trace = summary.assemble(fields, [three], DOCS)
        self.assertNotIn("фрагмент", json.dumps(trace, ensure_ascii=False).lower())


class WrappedAnswerTests(unittest.TestCase):
    """Ответ модели в обёртке ``{"field3": {...}}`` разбирается как плоский."""

    def test_unwrap(self):
        """Обёртка по имени блока снимается."""
        raw = '{"field3": {"notice": "а", "items": [{"numbers": "2.1", "text": "б"}]}}'
        self.assertEqual(summary.parse_block(raw)["notice"], "а")

    def test_flat_untouched(self):
        """Плоский ответ не меняется."""
        self.assertEqual(summary.parse_block('{"areas": [], "note": "", "recommendations": []}')["note"], "")

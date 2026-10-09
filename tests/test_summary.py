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

from knowledge_store import assessment, export, facts, repository as repo, summary
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
        self.assertEqual(data["field2_2_4_1"], 0)
        self.assertEqual(trace["field2_2_4_1"]["status"], "proposed")
        self.assertIsNone(data["field2_2_4_2"])           # «2» модели для критерия соответствия не принимается
        self.assertIn("оставлено эксперту", trace["field2_2_4_2"]["note"])
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
                         ("Услуги связи", "2325", 1500000.5, 30.0, "percent"))
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
            """Ответ модели: проверка замечания — «подтверждается», блок — текст."""
            if "verdict" in schema.get("properties", {}):
                return json.dumps({"verdict": "подтверждается"})
            return json.dumps({"text": "Вывод по замечанию."})

        with mock.patch.object(summary.Config, "PE_MODEL_ZEROS", "value"):
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

    def test_print_form_facts_refreshed_on_summary(self):
        """Факты раздела 1 берутся из печатной формы при каждой сборке, без пересчёта фактов модели."""
        form, c = "44fz_competition_obj6", self.conn
        notice = (Path(__file__).parent / "data" / "printform_notice.txt").read_text(encoding="utf-8")
        c._run(f"DELETE FROM pe_form_fields WHERE form_code = '{form}'")
        for key, n, label in [("k_email", 1, "1.4. Наличие информации об адресе электронной почты"),
                              ("k_spec", 2, "1.7. Наличие информации о специализированной организации")]:
            c._run(f"INSERT INTO pe_form_fields (form_code, field_key, ordinal, label, value_kind) "
                   f"VALUES ('{form}', '{key}', {n}, '{label}', 'presence')")
        c._run("INSERT INTO pe_procurements (expertise_id, law, method, object_code, check_type2) VALUES (8201, '44-ФЗ', 'Конкурс', 6, 1)")
        c._run("INSERT INTO pe_documents (expertise_id, doc_code, filename, sha256, text_full) VALUES "
               f"(8201, 'docIzvejenieFiles', 'Печатная-форма-извещения-(версия-1).html', 'pf1', $t${notice}$t$)")
        res = self.run_async(summary.generate_summary(c, 8201, None, None))
        self.assertEqual((res["data"]["k_email"], res["data"]["k_spec"]), (1, 2))
        self.assertEqual(res["trace"]["k_email"]["origin"], "print_form")
        self.assertEqual(res["trace"]["k_email"]["status"], "verified")
        self.assertEqual((res["stats"]["eis_xml"], res["stats"]["eis_print_form"]), (0, 2))
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
        """При методе рыночных цен критерии нормативного/затратного методов — 1 (proposed), замечаний нет."""
        fields = [field("m_norm", "compliance", "2.2.7. Соответствие законодательству расчета НМЦК нормативным методом"),
                  field("m_cost", "compliance", "2.2.7. Соответствие законодательству расчета НМЦК затратным методом"),
                  field("m_proj", "compliance", "2.2.7. Соответствие законодательству расчета НМЦК проектно-сметным методом")]
        docs = {1: {"filename": "o.xlsx", "doc_code": "docObosnovanie",
                    "text": "Используемый метод определения НМЦК с обоснованием Метод сопоставления рыночных цен (анализ рынка)"}}
        rows = [fact("m_norm", 0, verified=False), fact("m_cost", 0, verified=False), fact("m_proj", 0, verified=False)]
        data, trace = summary.assemble(fields, rows, docs)
        self.assertEqual((data["m_norm"], data["m_cost"], data["m_proj"]), (1, 1, 1))
        self.assertEqual(trace["m_norm"]["status"], "proposed")
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
        self.assertEqual(data["m_norm"], 1)
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


class BlockCleanTests(unittest.TestCase):
    """Очистка ответа модели и промпт по блокам."""

    def test_clean_notice(self):
        """Дубль вводной фразы и хвост «…» убираются."""
        text = summary.clean_notice("Информация, представленная в извещении № 1, соответствует требованиям законодательства, за исключением отсутствия адреса, о запрете…")
        self.assertTrue(text.startswith("отсутствия адреса"))
        self.assertFalse(text.endswith("…"))

    def test_clean_numbers(self):
        """Оборванный номер отбрасывается."""
        self.assertEqual(summary.clean_numbers("1.4,1.6,1.3…"), "1.4, 1.6")

    def test_prompt_for(self):
        """В промпте блока нет раздела другого блока."""
        p = summary.PROMPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("# БЛОК «ЗАКЛЮЧЕНИЕ»", summary.prompt_for(p, "field3"))
        self.assertNotIn("# БЛОК «ВЫВОД»", summary.prompt_for(p, "field4"))


class FundingAdvanceTests(unittest.IsolatedAsyncioTestCase):
    """Способ финансирования и аванс: паспорт, затем извлечение моделью с проверкой цитаты."""

    FIELDS = [{"field_key": k, "value_kind": "text", "label": k} for k in ("field1_2", "field1_2_unit", "field1_3")]
    DOCS = {1: {"filename": "n.html", "doc_code": "docIzvejenieFiles",
                "text": "Источник финансирования контракта:\nСредства бюджетных учреждений\nРазмер аванса 30 процентов от цены"}}

    def _data(self):
        return {k["field_key"]: None for k in self.FIELDS}, {}

    async def test_from_passport(self):
        """Финансирование берётся из паспорта без обращения к модели."""
        data, trace = self._data()
        await summary.fill_funding_advance(self.FIELDS, data, trace, self.DOCS, {"funding": "за счёт федерального бюджета"}, None)
        self.assertEqual(data["field1_3"], "за счёт федерального бюджета")
        self.assertEqual(trace["field1_3"]["status"], "from_passport")

    async def test_llm_with_verified_quote(self):
        """Значения принимаются, если цитата найдена в тексте."""
        async def llm(messages, schema):
            return json.dumps({"funding": "за счет средств бюджетных учреждений", "funding_quote": "Средства бюджетных учреждений",
                               "advance": 30, "advance_unit": "%", "advance_quote": "Размер аванса 30 процентов"})
        data, trace = self._data()
        await summary.fill_funding_advance(self.FIELDS, data, trace, self.DOCS, {}, llm)
        self.assertEqual(data["field1_3"], "за счет средств бюджетных учреждений")
        self.assertEqual((data["field1_2"], data["field1_2_unit"]), (30.0, "percent"))

    async def test_llm_unverified_quote_rejected(self):
        """Выдуманная цитата — поле остаётся эксперту."""
        async def llm(messages, schema):
            return json.dumps({"funding": "из воздуха", "funding_quote": "такого текста нет в документах",
                               "advance": None, "advance_unit": None, "advance_quote": None})
        data, trace = self._data()
        await summary.fill_funding_advance(self.FIELDS, data, trace, self.DOCS, {}, llm)
        self.assertIsNone(data["field1_3"])


class AssessmentTests(unittest.IsolatedAsyncioTestCase):
    """Метод НМЦК (field2_2_2_0) и оценка стиля (field2_2_1_6) моделью."""

    FIELDS = [{"field_key": "field2_2_2_0", "value_kind": "choice", "label": "Выберите метод"},
              {"field_key": "field2_2_1", "value_kind": "section", "label": "2.1"},
              {"field_key": "field2_2_1_6", "value_kind": "compliance", "label": "2.1.6. Соответствие: единый стиль"}]

    def docs(self, text):
        return {1: {"filename": "d.docx", "doc_code": "docIzvejenieFiles", "text": text}}

    async def test_nmck_choice_by_text(self):
        """Метод из строки «Используемый метод определения НМЦК» → код выбора."""
        from knowledge_store import assessment
        found = await assessment.detect_nmck_choice(self.docs("Используемый метод определения НМЦК: затратный метод"), None)
        self.assertEqual(found["value"], 3)
        found = await assessment.detect_nmck_choice(self.docs("Используемый метод определения НМЦК: метод сопоставимых рыночных цен"), None)
        self.assertEqual(found["value"], 1)

    async def test_nmck_choice_llm_needs_quote(self):
        """Ответ модели без подтверждённой цитаты не принимается."""
        from knowledge_store import assessment
        async def llm(m, s):
            return json.dumps({"method": "тарифный", "quote": "нет такого текста"})
        fallback = await assessment.detect_nmck_choice(self.docs("Метод расчёта НМЦК указан в приложении"), llm)
        self.assertEqual((fallback["value"], fallback["source"]), (1, "default"))   # без цитаты — косвенный вывод, не пусто

    async def test_style_defect_and_clean(self):
        """0 — только при дефекте с подтверждённой цитатой; иначе 1 как предложение."""
        from knowledge_store import assessment
        text = "Срок оказания услуг 10 дней. Срок оказания услуг 30 дней."
        async def bad(m, s):
            return json.dumps({"defects": [{"quote": "Срок оказания услуг 30 дней", "issue": "Противоречие сроков"},
                                           {"quote": "выдуманная цитата", "issue": "Ложный"}]})
        async def clean(m, s):
            return json.dumps({"defects": []})
        p = await assessment.compute_assessed(self.FIELDS, self.docs(text), bad)
        self.assertEqual(p["field2_2_1_6"][0], 0)
        self.assertEqual(len(p["field2_2_1_6"][1]["defects"]), 1)
        p = await assessment.compute_assessed(self.FIELDS, self.docs(text), clean)
        self.assertEqual((p["field2_2_1_6"][0], p["field2_2_1_6"][1]["status"]), (1, "proposed"))

    async def test_preset_used_in_section(self):
        """Оценённое значение попадает в итог подраздела."""
        data, trace = summary.assemble(self.FIELDS, [], {}, None, {"field2_2_1_6": (0, {"status": "proposed"})})
        self.assertEqual(data["field2_2_1_6"], 0)
        self.assertEqual(data["field2_2_1"], 0)


class PresumptionTests(unittest.IsolatedAsyncioTestCase):
    """Презумпция соответствия для критериев 2.3.x."""

    FIELDS = [{"field_key": k, "value_kind": "compliance", "label": k}
              for k in ("field2_3_2", "field2_3_3", "field2_3_4", "field2_3_6", "field2_3_8")]
    TEXT = ("Наименование объекта закупки\nОказание услуг по организации спортивных мероприятий\n"
            "Размер неустойки (пени) устанавливается в размере 5 процентов от цены контракта за каждый день просрочки.")
    DOCS = {1: {"filename": "k.docx", "doc_code": "docIzvejenieFiles", "text": TEXT}}

    async def test_violation_with_quote_gives_zero(self):
        """Нарушение с подтверждённой цитатой → 0; выдуманная цитата не считается."""
        from knowledge_store import assessment

        async def llm(messages, schema):
            return json.dumps({"violations": [{"quote": "неустойки (пени) устанавливается в размере 5 процентов от цены контракта",
                                               "issue": "Размер пени превышает установленный постановлением № 1042"},
                                              {"quote": "нет такого текста", "issue": "Ложное"}]})
        p = await assessment.compute_assessed(self.FIELDS, self.DOCS, llm)
        value, entry = p["field2_3_3"]
        self.assertEqual(value, 0)
        self.assertEqual(len(entry["defects"]), 1)

    async def test_no_violation_gives_proposed_one(self):
        """Нарушений нет → 1 со статусом proposed."""
        from knowledge_store import assessment

        async def llm(messages, schema):
            return json.dumps({"violations": []})
        p = await assessment.compute_assessed(self.FIELDS, self.DOCS, llm)
        self.assertEqual((p["field2_3_3"][0], p["field2_3_3"][1]["status"]), (1, "proposed"))
        self.assertEqual(p["field2_3_6"][0], 1)          # положений о ПДн нет — презумпция

    async def test_education_not_applicable(self):
        """Объект не из сферы образования — 2.3.2 получает 1 с пояснением."""
        from knowledge_store import assessment
        p = await assessment.compute_assessed(self.FIELDS, self.DOCS, None)
        self.assertEqual(p["field2_3_2"][0], 1)
        self.assertNotIn("field2_3_3", p)                # без модели презумпция не ставится

    async def test_skip_decided_and_fill_if_empty(self):
        """Ключи, решённые поиском фактов, пропускаются; assemble не затирает уже решённое значение."""
        from knowledge_store import assessment
        async def llm(messages, schema):
            return json.dumps({"violations": []})
        p = await assessment.compute_assessed(self.FIELDS, self.DOCS, llm, skip=["field2_3_3", "field2_3_2"])
        self.assertNotIn("field2_3_3", p)
        self.assertNotIn("field2_3_2", p)
        facts = [{"fact_key": "field2_3_4", "value": {"value": 0, "verified": False, "comment": "x"}, "document_id": None,
                  "page": None, "quote": None, "source": "fact_extractor"}]
        data, trace = summary.assemble(self.FIELDS, facts, {}, None, {"field2_3_4": (1, {"status": "proposed"})})
        self.assertEqual(data["field2_3_4"], 0)


class ClipAndDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """Аккуратная обрезка текста и причины пустых полей в trace."""

    def test_clip_text_no_ellipsis_or_dangling(self):
        """Обрезка по границе фразы, без «…» и висячих предлогов."""
        text = "Отсутствуют сведения об адресе, ответственном лице, специализированной организации и о"
        out = summary.clip_text(text, 200)
        self.assertFalse(out.endswith(("о", "…", ",")))
        self.assertNotIn("…", summary.clip_text("а, б, в, г, д " * 30, 40))
        self.assertLessEqual(len(summary.clip_text("слово " * 100, 50)), 50)

    async def test_funding_failure_reason_in_trace(self):
        """Если значение не принято, в trace записана причина и ответ модели."""
        fields = [{"field_key": k, "value_kind": "text", "label": k} for k in ("field1_2", "field1_3")]
        docs = {1: {"filename": "n.html", "doc_code": "docIzvejenieFiles", "text": "Источник финансирования: бюджет. Размер аванса"}}
        async def llm(m, s):
            return json.dumps({"funding": "бюджет", "funding_quote": "нет в тексте", "funding_quote_x": 1,
                               "advance": None, "advance_unit": None, "advance_quote": None})
        data, trace = {"field1_2": None, "field1_3": None}, {}
        await summary.fill_funding_advance(fields, data, trace, docs, {}, llm)
        self.assertIsNone(data["field1_3"])
        self.assertIn("ответ модели", trace["field1_3"]["note"])

    async def test_presumption_error_in_trace(self):
        """Сбой модели при презумпции — причина в trace, значение остаётся эксперту."""
        from knowledge_store import assessment
        fields = [{"field_key": "field2_3_3", "value_kind": "compliance", "label": "x"}]
        docs = {1: {"filename": "k.docx", "doc_code": "docIzvejenieFiles", "text": "Размер неустойки (пени) 5 процентов"}}
        async def llm(m, s):
            return "не json"
        preset = await assessment.compute_assessed(fields, docs, llm)
        data, trace = summary.assemble(fields, [], docs, None, preset)
        self.assertIsNone(data["field2_3_3"])
        self.assertIn("не разобран", trace["field2_3_3"]["assessment_error"])


class RobustParsingTests(unittest.IsolatedAsyncioTestCase):
    """Разбор «неудобных» ответов модели и слабых цитат (прогон по закупке 0301100027726000035)."""

    def test_fenced_list_is_parsed(self):
        """Список в обёртке ```json с русскими ключами разбирается как нарушения."""
        raw = '```json\n[{"нарушение": "Нет неустойки", "цитата": "штраф"}]\n```'
        parsed = assessment._parse(raw)
        item = assessment._items(parsed, "violations")[0]
        self.assertEqual(assessment._pick(item, assessment._ISSUE_KEYS), "Нет неустойки")
        self.assertEqual(assessment._pick(item, assessment._QUOTE_KEYS), "штраф")
        self.assertEqual(assessment._parse("```json\n[]\n```"), {"items": []})

    def test_weak_quote(self):
        """Общие фразы — слабые цитаты, конкретный текст и пустая цитата — нет."""
        self.assertTrue(summary.weak_quote("Информация отсутствует"))
        self.assertTrue(summary.weak_quote("Обеспечение гарантийных обязательств не требуется"))
        self.assertFalse(summary.weak_quote("Срок оплаты не более 7 рабочих дней с даты подписания акта"))
        self.assertFalse(summary.weak_quote(None))

    def test_funding_aliases_and_print_form(self):
        """Русские имена полей приводятся к схеме; источник финансирования берётся из печатной формы."""
        parsed = summary._funding_aliases({"источник_финансирования": "Средства бюджетных учреждений", "размер_аванса": None})
        self.assertEqual(parsed["funding"], "Средства бюджетных учреждений")
        docs = {1: {"text": "Закупка за счет бюджетных средств\nНет\nЗакупка за счет собственных средств организации\nДа\n"}}
        value, quote = summary.funding_from_print_form(docs)
        self.assertEqual(value, "Закупка за счет собственных средств организации")
        self.assertIn("Да", quote)


class WeakZeroRemarksTests(unittest.TestCase):
    """«0» на общей фразе не становится нарушением в блоках III–IV."""

    def test_weak_zero_not_in_remarks(self):
        """collect_remarks пропускает критерии с trace.weak_zero."""
        fields = [{"field_key": "a", "label": "1.36. Размер", "value_kind": "presence"},
                  {"field_key": "b", "label": "1.37. Порядок", "value_kind": "presence"}]
        data = {"a": 0, "b": 0}
        trace = {"a": {"status": "proposed", "weak_zero": True}, "b": {"status": "verified", "comment": "нет"}}
        self.assertEqual([r["field_key"] for r in summary.collect_remarks(fields, data, trace)], ["b"])


class ExpenseTypeTests(unittest.TestCase):
    """Критерий 3.5: КВР из ИКЗ."""

    def docs(self, ikz):
        """Документ извещения с ИКЗ."""
        return {1: {"text": f"Идентификационный код закупки\n{ikz}\n", "filename": "f.html", "doc_code": "docIzvejenieFiles"}}

    def test_kvr_244_matches(self):
        """КВР 244 подходит к любой группе ОКПД2."""
        value, entry = assessment.assess_expense_type(self.docs("261245700735124570100100080016110244"))
        self.assertEqual(value, 1)
        self.assertIn("244", entry["comment"])
        self.assertEqual(entry["status"], "proposed")

    def test_kvr_mismatch_is_left_to_expert(self):
        """КВР 243 при ОКПД2 61.10 (связь) не сходится: значение не ставится."""
        self.assertIsNone(assessment.assess_expense_type(self.docs("261245700735124570100100080016110243")))
        self.assertEqual(assessment.assess_expense_type(self.docs("261245700735124570100100080014322243"))[0], 1)

    def test_no_ikz(self):
        """ИКЗ нет — решения нет."""
        self.assertIsNone(assessment.assess_expense_type({1: {"text": "текст"}}))


class AdvanceRublesTests(unittest.IsolatedAsyncioTestCase):
    """Аванс (field1_2) — в единицах извещения (percent/rub); «аванса нет» — 0; ответ модели с ключом ``answer``; «2» для соответствия."""

    def test_advance_unit(self):
        """Единица аванса: «%» → percent, «руб.» → rub."""
        self.assertEqual(summary.advance_unit("%"), "percent")
        self.assertEqual(summary.advance_unit("руб."), "rub")
        self.assertIsNone(summary.advance_unit("шт"))

    async def test_no_advance_is_zero(self):
        """Критерий 1.22 = «2» → field1_2 = 0 (даже без модели), единица — руб."""
        fields = [{"field_key": k, "value_kind": "number", "label": k} for k in ("field1_1", "field1_2", "field1_2_unit", "field2_1_22")]
        data = {"field1_1": 100.0, "field1_2": None, "field1_2_unit": None, "field2_1_22": 2}
        trace = {}
        await summary.fill_funding_advance(fields, data, trace, {}, {}, None)
        self.assertEqual(data["field1_2"], 0)
        self.assertEqual(data["field1_2_unit"], "rub")
        self.assertEqual(trace["field1_2"]["status"], "derived")

    def test_answer_key_alias(self):
        """Ответ ``{"answer": "2", ...}`` без ключа ``value`` принимается."""
        res = facts.validate_answer({"answer": "1", "quote": "", "fragment": "0", "comment": "ок"}, [])
        self.assertEqual(res["value"], 1)

    def test_not_applicable_only_where_experts_use_it(self):
        """«2» от модели для критерия 2.2.3 не принимается, для 4.3 — принимается; факты XML не ограничиваются."""
        self.assertIsNone(summary.usable_result("field2_2_3", "compliance", 2, "fact_extractor"))
        self.assertEqual(summary.usable_result("field2_4_3", "compliance", 2, "fact_extractor"), 2)
        self.assertEqual(summary.usable_result("field2_2_3", "compliance", 2, facts.SOURCE_EIS), 2)


class NmckClassifierTests(unittest.TestCase):
    """Метод НМЦК по косвенным признакам (field2_2_2_0 не должен быть пустым)."""

    def docs(self, *texts):
        """Документы с заданными текстами."""
        return {i: {"text": t, "filename": f"d{i}.docx"} for i, t in enumerate(texts, 1)}

    def test_market_by_commercial_offers(self):
        """Коммерческие предложения и коэффициент вариации → рыночный (1)."""
        r = assessment.classify_nmck_method(self.docs("Получены коммерческие предложения. Коэффициент вариации 12%"))
        self.assertEqual((r["value"], r["source"]), (1, "indirect"))

    def test_estimate_needs_two_markers_or_okpd(self):
        """Одно слово «сметная стоимость» не делает метод проектно-сметным; два маркера — делают."""
        self.assertEqual(assessment.classify_nmck_method(self.docs("сметная стоимость услуг"))["value"], 1)
        r = assessment.classify_nmck_method(self.docs("Локальная смета ЛСР № 1, ГЭСН 46"))
        self.assertEqual(r["value"], 5)

    def test_okpd_prior_and_default(self):
        """ИКЗ с ОКПД2 43.. → проектно-сметный; без признаков — рыночный по умолчанию, даже без текстов."""
        r = assessment.classify_nmck_method(self.docs("Идентификационный код\n261245700735124570100100080014322244\n"))
        self.assertEqual(r["value"], 5)
        d = assessment.classify_nmck_method({})
        self.assertEqual((d["value"], d["source"]), (1, "default"))

    def test_direct_method_still_wins(self):
        """Метод, названный прямо в тексте, выбирается без классификатора и подтверждается."""
        import asyncio
        found = asyncio.run(assessment.detect_nmck_choice(
            self.docs("Используемый метод определения НМЦК: Затратный метод"), None))
        self.assertEqual((found["value"], found["source"]), (3, "text_rule"))


class MethodFitTests(unittest.IsolatedAsyncioTestCase):
    """Критерий 2.2.3 опирается на определённый метод НМЦК, а не на комментарий про другой метод."""

    def test_method_conflict(self):
        """Комментарий про «нормативный метод» при рыночном методе — конфликт; про рыночный — нет."""
        self.assertEqual(assessment.method_conflict("что является нормативным методом", 1), "нормативный")
        self.assertIsNone(assessment.method_conflict("рыночный метод, коммерческие предложения", 1))
        self.assertIsNone(assessment.method_conflict("текст без методов", 1))
        self.assertIsNone(assessment.method_conflict("нормативным методом", None))

    async def test_preset_replaces_conflicting_fact(self):
        """Факт про другой метод не считается решением: критерий получает вывод по определённому методу."""
        fields = [{"field_key": k, "value_kind": "choice" if k.endswith("_0") else "compliance", "label": k}
                  for k in ("field2_2_2_0", "field2_2_2_3")]
        docs = {1: {"text": "Использованы коммерческие предложения, ценовая информация", "filename": "n.xlsx"}}
        preset = await assessment.compute_assessed(
            fields, docs, None, skip=["field2_2_2_3"],
            fact_texts={"field2_2_2_3": "цена рассчитывается нормативным методом по постановлению № 2604"})
        self.assertEqual(preset["field2_2_2_0"][0], 1)
        self.assertEqual(preset["field2_2_2_3"][0], 1)
        self.assertIn("рыночный", preset["field2_2_2_3"][1]["comment"])
        # факт без конфликта остаётся решением
        again = await assessment.compute_assessed(fields, docs, None, skip=["field2_2_2_3"],
                                                   fact_texts={"field2_2_2_3": "рыночный метод применён верно"})
        self.assertNotIn("field2_2_2_3", again)


class JudgeTests(unittest.TestCase):
    """Вторая проверка замечаний «0» от модели (judge) и её влияние на сборку."""

    FIELDS = [{"field_key": "k_cmp", "value_kind": "compliance", "label": "2.1.1. Соответствие", "ordinal": 1},
              {"field_key": "k_ns", "value_kind": "compliance", "label": "4.3. Требование", "ordinal": 2}]

    def row(self, key, comment, quote="цитата"):
        """Факт модели «0» для ключа."""
        return {"fact_key": key, "source": "fact_extractor", "document_id": 1, "page": 1, "quote": quote,
                "value": {"value": 0, "verified": True, "comment": comment}}

    def build(self, judged):
        """Сборка с вердиктами ``judged`` по двум критериям."""
        docs = {1: {"filename": "d.docx", "doc_code": "docIzvejenieFiles", "text": "цитата в тексте документа"}}
        rows = [self.row("k_cmp", "нарушено"), self.row("k_ns", "не требуется")]
        return summary.assemble(self.FIELDS, rows, docs, None, None, judged)

    def test_parse_verdict(self):
        """Разбор вердикта: объект, алиасы, «не подтверждается» не равно «подтверждается»."""
        self.assertEqual(assessment.parse_verdict('{"verdict": "подтверждается"}'), "supported")
        self.assertEqual(assessment.parse_verdict('{"вердикт": "Нет"}'), "unsupported")
        self.assertEqual(assessment.parse_verdict('```json\n{"verdict": "не применимо"}\n```'), "not_applicable")
        self.assertEqual(assessment.parse_verdict('{"verdict": "не подтверждается"}'), "unsupported")
        self.assertIsNone(assessment.parse_verdict("ерунда"))

    def test_supported_is_verified_zero(self):
        """Подтверждённый «0» — verified и попадает в замечания."""
        data, trace = self.build({"k_cmp": "supported", "k_ns": "supported"})
        self.assertEqual(data["k_cmp"], 0)
        self.assertEqual(trace["k_cmp"]["status"], "verified")
        self.assertEqual(trace["k_cmp"]["judged"], "supported")

    def test_unsupported_and_not_applicable(self):
        """Отклонённый «0» → «1» (proposed); «не применимо» → «2» только где допустимо, иначе эксперту."""
        data, trace = self.build({"k_cmp": "unsupported", "k_ns": "not_applicable"})
        self.assertEqual(data["k_cmp"], 1)
        self.assertEqual(trace["k_cmp"]["status"], "proposed")
        self.assertIsNone(data["k_ns"])
        self.assertEqual(trace["k_ns"]["judged"], "not_applicable")

    def test_no_verdict_is_not_a_remark(self):
        """Нет вердикта: «0» — предложение и не идёт в замечания; без проверки (None) поведение прежнее."""
        data, trace = self.build({})
        self.assertEqual(data["k_cmp"], 0)
        self.assertEqual(trace["k_cmp"]["status"], "proposed")
        self.assertEqual(summary.collect_remarks(self.FIELDS, data, trace), [])
        data, trace = self.build(None)
        self.assertEqual(trace["k_cmp"]["status"], "verified")

    def test_judge_zeros_calls_model_per_zero(self):
        """Каждый «0» модели проверяется отдельным вызовом; факты ЕИС не проверяются."""
        calls = []

        async def llm(messages, schema):
            """Заглушка модели: «нет» для всех."""
            calls.append(messages[-1]["content"])
            return '{"verdict": "нет"}'

        docs = {1: {"filename": "d.docx", "text": "цитата в тексте документа"}}
        rows = [self.row("k_cmp", "нарушено"), dict(self.row("k_ns", "x"), source="eis_xml")]
        out = asyncio.run(assessment.judge_zeros(self.FIELDS, rows, docs, llm))
        self.assertEqual(out, {"k_cmp": "unsupported"})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(asyncio.run(assessment.judge_zeros(self.FIELDS, rows, docs, None)))


class NotSetAndAdvanceTests(unittest.TestCase):
    """4.3–4.5: «требование не установлено» → «2»; 1.22 по модели — только с размером аванса в цитате."""

    def build(self, key, label, comment, quote, value=0):
        """Сборка одного критерия по факту модели."""
        fields = [{"field_key": key, "value_kind": "compliance", "label": label, "ordinal": 1}]
        row = {"fact_key": key, "source": "fact_extractor", "document_id": 1, "page": 1, "quote": quote,
               "value": {"value": value, "verified": True, "comment": comment}}
        docs = {1: {"filename": "d.docx", "doc_code": "docIzvejenieFiles", "text": f"текст {quote} конец"}}
        return summary.assemble(fields, [row], docs)

    def test_requirement_not_set_is_two(self):
        """«Требование о лицензии не установлено» для 4.4 — «2» (proposed), а не нарушение."""
        data, trace = self.build("field2_4_4", "4.4. Лицензия", "требование о лицензии не установлено", "лицензия не требуется")
        self.assertEqual(data["field2_4_4"], 2)
        self.assertEqual(trace["field2_4_4"]["status"], "proposed")

    def test_real_violation_stays_zero(self):
        """Настоящее нарушение в 4.4 остаётся «0»."""
        data, _ = self.build("field2_4_4", "4.4. Лицензия", "лицензия указана без вида деятельности", "наличие лицензии")
        self.assertEqual(data["field2_4_4"], 0)

    def test_advance_template_phrase_is_not_amount(self):
        """1.22: фраза «если предусмотрен аванс» без числа → «2» (proposed); с числом — «1»."""
        data, trace = self.build("field2_1_22", "1.22. Размер аванса", "указан", "если предусмотрен аванс", value=1)
        self.assertEqual(data["field2_1_22"], 2)
        self.assertEqual(trace["field2_1_22"]["status"], "proposed")
        data, _ = self.build("field2_1_22", "1.22. Размер аванса", "указан", "размер аванса 30 % от цены", value=1)
        self.assertEqual(data["field2_1_22"], 1)


class ContractCheckTests(unittest.TestCase):
    """Правила нарушений проекта контракта (2.5) и вывод 3.7 из 2.5."""

    BODY = ("Заказчик оплачивает услуги по мере поступления денежных средств. Исполнение контракта обеспечивается "
            "банковской гарантией. Приёмка оформляется актом по форме КС-2. Штраф по ПП № 1042. ") * 60

    def test_detects_typical_violations(self):
        """Находит оплату «по мере поступления», банковскую гарантию, отсутствие электронной приёмки и уведомлений."""
        from knowledge_store import contract_check
        codes = {d["code"] for d in contract_check.check_contract(self.BODY)}
        self.assertTrue({"pay_on_funds", "bank_guarantee", "e_acceptance", "e_notices"} <= codes)

    def test_short_text_and_clean_contract(self):
        """Короткий текст проверок не вызывает; контракт с ЕИС-приёмкой и уведомлениями — чистый."""
        from knowledge_store import contract_check
        self.assertEqual(contract_check.check_contract("короткий"), [])
        clean = ("Приёмка: заказчик формирует в единой информационной системе документ о приёмке. При применении мер "
                 "ответственности стороны обмениваются документами в единой информационной системе путём электронных "
                 "уведомлений. Штраф по ПП № 1042. ") * 80
        self.assertEqual(contract_check.check_contract(clean), [])

    def test_price_right_only_in_competition(self):
        """Ответственность за «цену за право заключения» — нарушение только в конкурсе."""
        from knowledge_store import contract_check
        text = (self.BODY + "победителем, предложившим наиболее высокую цену за право заключения контракта. ") * 2
        self.assertIn("price_right_fine", {d["code"] for d in contract_check.check_contract(text, competition=True)})
        self.assertNotIn("price_right_fine", {d["code"] for d in contract_check.check_contract(text, competition=False)})

    def test_rule_overrides_one_and_derives_37(self):
        """Правило даёт 2.5 = 0 вместо «1» модели; 3.7 выводится из 2.5 и не дублируется в замечаниях."""
        fields = [{"field_key": "field2_2_5", "value_kind": "compliance", "label": "2.5. Проект контракта", "ordinal": 1},
                  {"field_key": "field2_3_7", "value_kind": "compliance", "label": "3.7. Документация", "ordinal": 2}]
        docs = {1: {"filename": "Проект контракта.docx", "doc_code": None, "text": self.BODY}}
        rows = [{"fact_key": "field2_2_5", "source": "fact_extractor", "document_id": 1, "page": 1, "quote": None,
                 "value": {"value": 1, "verified": True, "comment": None}}]
        preset = asyncio.run(assessment.compute_assessed(fields, docs, None))
        data, trace = summary.assemble(fields, rows, docs, None, preset)
        self.assertEqual((data["field2_2_5"], data["field2_3_7"]), (0, 0))
        self.assertEqual(trace["field2_3_7"]["derived_from"], "field2_2_5")
        remarks = summary.collect_remarks(fields, data, trace)
        self.assertEqual([r["field_key"] for r in remarks], ["field2_2_5"])

    def test_only_weak_findings_give_no_zero(self):
        """Только слабые признаки (уведомления, «цена за право») значение 2.5 не определяют."""
        text = ("Приёмка: заказчик формирует в единой информационной системе документ о приёмке. Штраф по ПП № 1042, "
                "победителем, предложившим наиболее высокую цену за право заключения контракта. ") * 80
        docs = {1: {"filename": "Проект контракта.docx", "text": text}}
        self.assertIsNone(assessment.assess_contract(docs, True))


class DocumentTitlesTests(unittest.TestCase):
    """Поле documents: названия из самих документов с реквизитами."""

    XML = ('<?xml version="1.0" encoding="UTF-8"?><ns3:export xmlns:ns3="a" xmlns:ns5="b"><ns3:epProtocolEOK2020Final>'
           '<ns5:commonInfo><ns5:purchaseNumber>0334100018225000001</ns5:purchaseNumber><ns5:docNumber>ИЭОК1</ns5:docNumber>'
           '<ns5:publishDTInEIS>2025-09-02T11:38:40+08:00</ns5:publishDTInEIS></ns5:commonInfo>'
           '</ns3:epProtocolEOK2020Final></ns3:export>')

    def test_xml_title_has_number_and_date(self):
        """Протокол из XML: вид, дата и номер документа."""
        from knowledge_store import doc_titles
        self.assertEqual(doc_titles.xml_title(self.XML),
                         "Протокол подведения итогов определения поставщика (подрядчика, исполнителя) от 02.09.2025 №ИЭОК1")
        self.assertIsNone(doc_titles.xml_title("обычный текст"))
        self.assertIsNone(doc_titles.xml_title(self.XML.replace("epProtocolEOK2020Final", "fcsPlacementResult")))

    def test_list_uses_titles_from_documents(self):
        """Перечень: модель называет документ по тексту; выдуманное название отбрасывается; порядок и дубли."""
        from knowledge_store import doc_titles

        async def llm(messages, schema):
            """Модель: для «Описания» — верное название, для остальных — выдуманное."""
            if "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ" in messages[-1]["content"]:
                return '{"title": "Описание объекта закупки (Приложение 1)"}'
            return '{"title": "Совершенно другой документ про космос"}'

        docs = [
            {"filename": "Протокол.xml", "text": self.XML},
            {"filename": "ТЗ.docx", "text": "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ (Приложение 1)\\nна оказание услуг"},
            {"filename": "Порядок.docx", "text": "Приложение 5\nПорядок рассмотрения и оценки заявок\nтекст"},
            {"filename": "Порядок_1.docx", "text": "Приложение 5\nПорядок рассмотрения и оценки заявок\nтекст"},
            {"filename": "ЭЗ_5110_эксперт_30.pdf", "text": "заключение"},
            {"filename": "Итоги.xml", "text": self.XML.replace("epProtocolEOK2020Final", "fcsPlacementResult")},
        ]
        items = asyncio.run(doc_titles.document_titles(docs, llm))
        titles = [i["title"] for i in items]
        self.assertEqual(titles[0], "Описание объекта закупки (Приложение 1)")
        self.assertEqual(titles[1], "Порядок рассмотрения и оценки заявок")
        self.assertEqual(len(titles), 3)          # дубль объединён, ЭЗ и итоги по лотам не включены
        self.assertTrue(titles[2].startswith("Протокол подведения итогов"))
        self.assertEqual(doc_titles.format_titles(items).splitlines()[0], "1. Описание объекта закупки (Приложение 1).")


class SuspectZerosTests(unittest.TestCase):
    """Режим PE_MODEL_ZEROS=suspect: «0» модели не становится значением, а остаётся предположением в trace."""

    FIELDS = [{"field_key": "k_cmp", "value_kind": "compliance", "label": "2.1.1. Соответствие", "ordinal": 1},
              {"field_key": "k_pres", "value_kind": "presence", "label": "1.5. Наличие", "ordinal": 2},
              {"field_key": "field2_4_4", "value_kind": "compliance", "label": "4.4. Лицензия", "ordinal": 3}]

    def row(self, key, comment, quote="цитата"):
        """Факт модели «0»."""
        return {"fact_key": key, "source": "fact_extractor", "document_id": 1, "page": 1, "quote": quote,
                "value": {"value": 0, "verified": True, "comment": comment}}

    def build(self, mode):
        """Сборка трёх фактов «0» в заданном режиме."""
        docs = {1: {"filename": "d.docx", "doc_code": "docIzvejenieFiles", "text": "цитата в тексте документа"}}
        rows = [self.row("k_cmp", "нарушено"), self.row("k_pres", "нет сведений"),
                self.row("field2_4_4", "лицензия не требуется")]
        return summary.assemble(self.FIELDS, rows, docs, None, None, None, mode)

    def test_suspect_mode(self):
        """Соответствие → «1» (proposed), наличие → эксперту, предположение сохранено; «не требуется» → «2»."""
        data, trace = self.build("suspect")
        self.assertEqual(data["k_cmp"], 1)
        self.assertEqual(trace["k_cmp"]["status"], "proposed")
        self.assertEqual(trace["k_cmp"]["suspected_zero"]["comment"], "нарушено")
        self.assertIsNone(data["k_pres"])
        self.assertIn("suspected_zero", trace["k_pres"])
        self.assertEqual(data["field2_4_4"], 2)
        self.assertEqual(summary.collect_remarks(self.FIELDS, data, trace), [])

    def test_value_mode_keeps_zero(self):
        """Режим value: прежнее поведение — «0» подтверждённой цитатой остаётся значением."""
        data, _ = self.build("value")
        self.assertEqual(data["k_cmp"], 0)

    def test_preset_zero_becomes_suspect(self):
        """«0» презумпции в режиме suspect заменяется «1», находка уходит в suspected_zero; правила контракта не затрагиваются."""
        fields = [{"field_key": "field2_3_3", "value_kind": "compliance", "label": "3.3.", "ordinal": 1},
                  {"field_key": "field2_2_5", "value_kind": "compliance", "label": "2.5.", "ordinal": 2}]
        preset = {"field2_3_3": (0, {"status": "proposed", "source": "llm_presumption", "quote": "q", "comment": "нарушение",
                                     "defects": [{"issue": "x", "quote": "q"}]}),
                  "field2_2_5": (0, {"status": "proposed", "source": "contract_rules", "quote": None, "comment": "нет ЕИС"})}
        data, trace = summary.assemble(fields, [], {1: {"filename": "d", "text": "t"}}, None, preset, None, "suspect")
        self.assertEqual((data["field2_3_3"], data["field2_2_5"]), (1, 0))
        self.assertEqual(trace["field2_3_3"]["suspected_zero"]["comment"], "нарушение")

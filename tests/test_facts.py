"""Тесты этапа 4: правила по XML извещения ЕИС, валидатор цитат, FactExtractor, запись фактов.

LLM, эмбеддинги и векторный поиск подменяются заглушками; SQL фактов выполняется на настоящем
Postgres, если задана переменная ``PE_TEST_PG=host:port`` (pgvector в песочнице нет, поэтому SQL
поиска по вектору проверяется только на dev).
"""
import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from knowledge_store import eis_notice, facts, hooks, indexer, package_findings, repository as repo
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

NS = ('xmlns:ns3="http://zakupki.gov.ru/oos/export/1" xmlns:ns5="http://zakupki.gov.ru/oos/EPtypes/1" '
      'xmlns:ns2="http://zakupki.gov.ru/oos/base/1" xmlns:ns4="http://zakupki.gov.ru/oos/common/1"')


def make_xml(version=1, role="CU", account="40702810000000000001", multi_not_provided="true",
             stages=1, advance=None, procedure="Порядок внесения указан в извещении"):
    """Собирает минимальное XML-извещение с пространствами имён ЕИС."""
    stage = "<ns5:stageInfo><ns5:termsInfo><ns5:notRelativeTermsInfo><ns5:endDate>2026-08-29</ns5:endDate>" \
            "</ns5:notRelativeTermsInfo></ns5:termsInfo><ns5:financeInfo><ns5:total>100.5</ns5:total></ns5:financeInfo></ns5:stageInfo>"
    adv = f"<ns5:advancePaymentSum><ns5:sumInPercents>{advance}</ns5:sumInPercents></ns5:advancePaymentSum>" if advance else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ns3:export {NS}><ns3:epNotificationEOK2020>
 <ns5:versionNumber>{version}</ns5:versionNumber>
 <ns5:commonInfo><ns5:purchaseObjectInfo>оказание услуг</ns5:purchaseObjectInfo>
   <ns5:placingWay><ns2:name>Открытый конкурс в электронной форме</ns2:name></ns5:placingWay></ns5:commonInfo>
 <ns5:purchaseResponsibleInfo><ns5:responsibleRole>{role}</ns5:responsibleRole>
   <ns5:responsibleOrgInfo><ns5:fullName>ФГБОУ ВО Университет</ns5:fullName></ns5:responsibleOrgInfo>
   <ns5:responsibleInfo><ns5:contactEMail>a@b.ru</ns5:contactEMail></ns5:responsibleInfo></ns5:purchaseResponsibleInfo>
 <ns5:notificationInfo>
  <ns5:procedureInfo><ns5:collectingInfo><ns5:endDT>2026-06-08T09:55:00</ns5:endDT></ns5:collectingInfo></ns5:procedureInfo>
  <ns5:contractConditionsInfo><ns5:contractMultiInfo><ns5:notProvided>{multi_not_provided}</ns5:notProvided></ns5:contractMultiInfo></ns5:contractConditionsInfo>
  <ns5:customerRequirementsInfo><ns5:customerRequirementInfo>
    <ns5:applicationGuarantee><ns5:amount>33581.34</ns5:amount>
      <ns5:procedureInfo>{procedure}</ns5:procedureInfo>
      <ns5:account><ns5:settlementAccount>{account}</ns5:settlementAccount></ns5:account></ns5:applicationGuarantee>
    <ns5:contractConditionsInfo>{adv}
      <ns5:contractExecutionPaymentPlan><ns5:stagesInfo>{stage * stages}</ns5:stagesInfo></ns5:contractExecutionPaymentPlan>
    </ns5:contractConditionsInfo>
  </ns5:customerRequirementInfo></ns5:customerRequirementsInfo>
 </ns5:notificationInfo></ns3:epNotificationEOK2020></ns3:export>"""


class EisRulesTests(unittest.TestCase):
    """Правила «XML → критерии раздела 1»."""

    def run_rules(self, **kw):
        """Разбирает синтетическое извещение и применяет все правила."""
        return eis_notice.evaluate_all(eis_notice.Notice.from_xml(make_xml(**kw)))

    def test_present_with_evidence(self):
        """Критерий в наличии: значение 1 и доказательство «путь = значение»."""
        f = self.run_rules()["1.1"]
        self.assertEqual(f.value, 1)
        self.assertIn("ФГБОУ ВО Университет", f.evidence)

    def test_namespaces_ignored(self):
        """Префиксы пространств имён не важны, поиск идёт по локальным именам."""
        self.assertEqual(self.run_rules()["1.9"].value, 1)

    def test_placeholder_account_is_not_information(self):
        """Счёт из одних нулей — заглушка: критерий 1.33 не засчитывается как «в наличии»."""
        self.assertEqual(self.run_rules()["1.33"].value, 1)
        self.assertIsNone(self.run_rules(account="00000000000000000000")["1.33"].value)

    def test_not_provided_and_undecided(self):
        """notProvided=true → «не предусмотрено» (2); нерешаемые критерии → None."""
        r = self.run_rules()
        self.assertEqual(r["1.39"].value, 2)
        self.assertIsNone(r["1.38"].value)
        self.assertIsNone(r["1.46"].value)

    def test_advance_absent_means_not_provided(self):
        """Нет аванса в XML → 2; есть — 1."""
        self.assertEqual(self.run_rules()["1.22"].value, 2)
        self.assertEqual(self.run_rules(advance="30.0")["1.22"].value, 1)

    def test_stages(self):
        """Этапы исполнения: один этап → «не предусмотрено», несколько → в наличии."""
        self.assertEqual(self.run_rules(stages=1)["1.17"].value, 2)
        self.assertEqual(self.run_rules(stages=3)["1.17"].value, 1)
        self.assertEqual(self.run_rules(stages=3)["1.19"].value, 1)

    def test_specialized_org(self):
        """Ответственный не заказчик (роль не CU) → специализированная организация привлечена."""
        self.assertEqual(self.run_rules(role="CU")["1.7"].value, 2)
        self.assertEqual(self.run_rules(role="OA")["1.7"].value, 1)

    def test_reference_to_regulation_is_not_decided(self):
        """«В соответствии с регламентом площадки» — не порядок: решает эксперт."""
        self.assertEqual(self.run_rules()["1.32"].value, 1)
        self.assertIsNone(self.run_rules(procedure="в соответствии с регламентом электронной площадки")["1.32"].value)

    def test_latest_version_wins(self):
        """Из нескольких версий извещения выбирается последняя."""
        notice = eis_notice.latest_notice([make_xml(version=1), make_xml(version=3), b"not xml", make_xml(version=2)])
        self.assertEqual(notice.version(), 3)
        self.assertIsNone(eis_notice.latest_notice([b"<a/>", "bad"]))

    def test_bad_xml_raises(self):
        """Не XML и XML без извещения — ValueError."""
        with self.assertRaises(ValueError):
            eis_notice.Notice.from_xml("не xml")
        with self.assertRaises(ValueError):
            eis_notice.Notice.from_xml("<a><b/></a>")

    def test_mapping_uses_label_number_and_hint(self):
        """Поле сопоставляется по номеру и тексту критерия; при сдвиге нумерации правило не применяется."""
        data = eis_notice.findings_to_json(eis_notice.Notice.from_xml(make_xml()), self.run_rules())
        fields = [
            {"field_key": "k_name", "label": "1.1. Наличие информации о наименовании Заказчика", "value_kind": "presence"},
            {"field_key": "k_shift", "label": "1.1. Наличие информации о чём-то другом", "value_kind": "presence"},
            {"field_key": "k_comp", "label": "1.1. Соответствие наименования", "value_kind": "compliance"},
        ]
        mapped = eis_notice.map_to_fields(data, fields)
        self.assertEqual(set(mapped), {"k_name"})
        self.assertEqual(mapped["k_name"].value, 1)

    def test_json_contains_only_decided(self):
        """В JSON для БД нет нерешённых критериев; номер версии сохраняется."""
        data = eis_notice.findings_to_json(eis_notice.Notice.from_xml(make_xml(version=2)), self.run_rules())
        self.assertEqual(data["version"], 2)
        self.assertNotIn("1.46", data["criteria"])
        self.assertIn("1.1", data["criteria"])


class QuoteValidationTests(unittest.TestCase):
    """Проверка цитат и ответов модели."""

    FR = [facts.Fragment(1, 10, 3, "Заказчик:  ФГБОУ «Университет»\nИНН 123"), facts.Fragment(2, 11, 5, "Срок — 10 дней, ёлка")]

    def test_quote_normalization(self):
        """Регистр, пробелы, кавычки, тире и ё не мешают найти дословную цитату."""
        self.assertTrue(facts.quote_in_text('заказчик: фгбоу "университет" инн 123', self.FR[0].text))
        self.assertTrue(facts.quote_in_text("срок - 10 дней, елка", self.FR[1].text))
        self.assertFalse(facts.quote_in_text("срок 30 дней", self.FR[1].text))
        self.assertFalse(facts.quote_in_text("", self.FR[1].text))

    def test_value_one_requires_verified_quote(self):
        """Ответ «1» без цитаты или с выдуманной цитатой отвергается."""
        self.assertIsNone(facts.validate_answer({"value": 1, "fragment": 1, "quote": "", "comment": "x"}, self.FR))
        self.assertIsNone(facts.validate_answer({"value": 1, "fragment": 1, "quote": "несуществующий текст", "comment": ""}, self.FR))

    def test_quote_found_in_other_fragment(self):
        """Если модель ошиблась номером фрагмента, цитата ищется в остальных; страница берётся у найденного."""
        r = facts.validate_answer({"value": 1, "fragment": 1, "quote": "Срок — 10 дней", "comment": "ок"}, self.FR)
        self.assertEqual((r["value"], r["verified"], r["document_id"], r["page"]), (1, True, 11, 5))

    def test_zero_and_two_without_quote(self):
        """«0» и «2» принимаются без цитаты, но помечаются как непроверенные."""
        r = facts.validate_answer({"value": 0, "fragment": 0, "quote": "", "comment": "нет сведений"}, self.FR)
        self.assertEqual((r["value"], r["verified"], r["quote"]), (0, False, None))
        self.assertEqual(facts.validate_answer({"value": 2, "quote": "", "fragment": 0, "comment": ""}, self.FR)["value"], 2)

    def test_garbage_answers(self):
        """Недопустимые значения и мусор не принимаются."""
        self.assertIsNone(facts.validate_answer({"value": 7, "quote": "x"}, self.FR))
        self.assertIsNone(facts.validate_answer({"value": "abc"}, self.FR))
        self.assertIsNone(facts.validate_answer(None, self.FR))
        self.assertIsNone(facts.parse_answer("не json"))
        self.assertEqual(facts.parse_answer('{"value": 1}'), {"value": 1})


class FakeEmbedder:
    """Эмбеддер-заглушка: запоминает запросы."""

    def __init__(self):
        """Создаёт пустой журнал запросов."""
        self.queries = []

    async def get_embeddings(self, texts):
        """Возвращает нулевые векторы по числу текстов."""
        self.queries += texts
        return np.zeros((len(texts), 3))


class ExtractorTests(unittest.TestCase):
    """FactExtractor с заглушками LLM и поиска."""

    def make(self, answers, fragments=None):
        """Создаёт экстрактор; ``answers`` — список ответов LLM по порядку вызовов."""
        self.calls = []
        frs = fragments if fragments is not None else [facts.Fragment(1, 10, 2, "Предмет контракта: поставка серверов")]

        async def llm(messages, schema):
            """Возвращает следующий заготовленный ответ."""
            self.calls.append(messages)
            return answers[len(self.calls) - 1]

        async def search(conn, expertise_id, vector, k):
            """Возвращает заготовленные фрагменты."""
            return frs

        self.emb = FakeEmbedder()
        return facts.FactExtractor(llm, self.emb, search=search, system_prompt="SYS")

    def test_query_has_no_number_and_prompt_has_fragment(self):
        """Поисковый запрос — текст критерия без номера; фрагмент и критерий попадают в промпт."""
        ex = self.make([json.dumps({"value": 1, "fragment": 1, "quote": "поставка серверов", "comment": "ок"})])
        field = {"field_key": "k", "label": "2.1.1. Соответствие наименований", "value_kind": "compliance"}
        fact = asyncio.run(ex.extract_field(None, 5, field))
        self.assertEqual(self.emb.queries, ["Соответствие наименований"])
        user = self.calls[0][1]["content"]
        self.assertIn("поставка серверов", user)
        self.assertIn("2.1.1. Соответствие наименований", user)
        self.assertEqual((fact["fact_key"], fact["value"], fact["verified"], fact["page"]), ("k", 1, True, 2))

    def test_no_fragments_means_no_fact(self):
        """Если текстов нет (например, удалены по сроку хранения), LLM не вызывается."""
        ex = self.make([], fragments=[])
        field = {"field_key": "k", "label": "1.1. Наличие", "value_kind": "presence"}
        self.assertIsNone(asyncio.run(ex.extract_field(None, 5, field)))
        self.assertEqual(self.calls, [])


FIELDS = [
    {"field_key": "k1", "label": "1.1. Наличие информации о наименовании Заказчика", "value_kind": "presence"},
    {"field_key": "k2", "label": "1.38.  Наличие информации о банковском   сопровождении", "value_kind": "presence"},
    {"field_key": "k3", "label": "2.1.1. Соответствие наименований", "value_kind": "compliance"},
    {"field_key": "k4", "label": "2.1.2. Соответствие характеристик", "value_kind": "compliance"},
]


def fact(key, value, verified=True, source="fact_extractor", quote=None, comment=None):
    """Собирает запись pe_facts для тестов дайджеста."""
    return {"fact_key": key, "value": {"value": value, "verified": verified, "comment": comment},
            "quote": quote, "page": 3, "source": source}


class DigestTests(unittest.TestCase):
    """Дайджест фактов и его сжатие для LLM."""

    def test_stats_and_missing(self):
        """Счётчики по значениям, источникам и подтверждению; «0» попадает в missing с доказательством."""
        digest = package_findings.build_digest([
            fact("k1", 1, source="eis_xml", quote="fullName = Университет"),
            fact("k2", 0, verified=False, comment="сведений нет"),
            fact("k3", 0, quote="Наименование: сервер"),
            fact("k4", 2),
            fact("unknown_key", 1),                       # нет в форме — игнорируется
        ], FIELDS, "44fz_competition_obj6")
        st = digest["stats"]
        self.assertEqual((st["checked"], st["present"], st["missing"], st["not_provided"]), (4, 1, 2, 1))
        self.assertEqual((st["from_xml"], st["from_llm"], st["verified"]), (1, 3, 3))
        self.assertEqual([m["field_key"] for m in digest["missing"]], ["k2", "k3"])
        self.assertEqual(digest["missing"][0]["verdict"], "отсутствует")
        self.assertEqual(digest["missing"][1]["verdict"], "не соответствует")
        self.assertEqual(digest["missing"][0]["criterion"], "1.38. Наличие информации о банковском сопровождении")
        self.assertEqual([u["field_key"] for u in digest["unverified"]], ["k2"])

    def test_json_string_values_and_garbage(self):
        """Значение из БД может прийти строкой JSON; мусор пропускается без исключений."""
        rows = [{"fact_key": "k1", "value": json.dumps({"value": 0, "verified": True}), "quote": None, "source": "eis_xml"},
                {"fact_key": "k2", "value": "не json", "quote": None, "source": "eis_xml"},
                {"fact_key": "k3", "value": {"value": 9}, "quote": None, "source": "eis_xml"}]
        digest = package_findings.build_digest(rows, FIELDS, "f")
        self.assertEqual(digest["stats"]["checked"], 1)
        self.assertEqual(digest["stats"]["missing"], 1)

    def test_compact_for_llm_limits(self):
        """Для промпта берётся не больше limit критериев, цитаты обрезаются."""
        many = [dict(field_key=f"k{i}", label=f"1.{i}. Наличие информации {'x' * 300}", value_kind="presence") for i in range(40)]
        digest = package_findings.build_digest([fact(f"k{i}", 0, quote="q" * 400) for i in range(40)], many, "f")
        compact = package_findings.compact_for_llm(digest, limit=5)
        self.assertEqual(len(compact["missing"]), 5)
        self.assertEqual(compact["missing_count"], 40)
        self.assertLessEqual(len(compact["missing"][0]["criterion"]), 160)
        self.assertLessEqual(len(compact["missing"][0]["evidence"]), 120)


class AttachTests(unittest.TestCase):
    """Присоединение дайджеста к входу и ответу пайплайна."""

    DIGEST = package_findings.build_digest([fact("k1", 0)], FIELDS, "f")

    def test_facts_go_first_and_none_is_noop(self):
        """Ключ facts ставится первым (ConsistencyChecker читает только начало); без дайджеста вход не меняется."""
        data = {"missed_documents": [], "documents": [{"a": 1}]}
        hooks.attach_facts_to_input(data, None)
        self.assertEqual(list(data), ["missed_documents", "documents"])
        hooks.attach_facts_to_input(data, self.DIGEST)
        self.assertEqual(list(data), ["facts", "missed_documents", "documents"])
        self.assertEqual(data["facts"]["missing_count"], 1)


class FlagsTests(unittest.TestCase):
    """Флаги и защита этапа фактов."""

    def test_both_flags_required(self):
        """Факты включаются только при KNOWLEDGE_STORE_ENABLED и PE_FACTS_ENABLED одновременно."""
        for store, facts_flag, expected in (("true", "true", True), ("true", "false", False), ("false", "true", False)):
            with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": store, "PE_FACTS_ENABLED": facts_flag}):
                self.assertEqual(hooks.facts_enabled(), expected)

    def test_disabled_does_not_touch_anything(self):
        """При выключенном флаге build_facts возвращает None и не вызывает ни индексацию, ни LLM."""
        with mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true", "PE_FACTS_ENABLED": "false"}), \
                mock.patch.object(hooks, "_build_facts", side_effect=AssertionError("не должно вызываться")):
            self.assertIsNone(asyncio.run(hooks.build_facts(1, None, None, "m")))

    def test_timeout_and_errors_return_none(self):
        """Превышение времени и исключение внутри этапа не ломают пайплайн: результат None."""
        async def slow(*args):
            """Имитирует зависший этап."""
            await asyncio.sleep(5)

        async def boom(*args):
            """Имитирует ошибку этапа."""
            raise RuntimeError("сбой")

        env = {"KNOWLEDGE_STORE_ENABLED": "true", "PE_FACTS_ENABLED": "true"}
        with mock.patch.dict(os.environ, env), mock.patch.object(hooks.Config, "PE_FACTS_TIMEOUT_SEC", 0.2):
            with mock.patch.object(hooks, "_build_facts", slow):
                self.assertIsNone(asyncio.run(hooks.build_facts(1, None, None, "m")))
            with mock.patch.object(hooks, "_build_facts", boom):
                self.assertIsNone(asyncio.run(hooks.build_facts(1, None, None, "m")))


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class FactsDbTests(unittest.TestCase):
    """extract_facts и хук разбора XML на настоящем Postgres."""

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

    def q(self, sql):
        """Выполняет SELECT и возвращает stdout."""
        return self.conn._run(sql)

    def test_hook_parses_xml_and_extract_facts(self):
        """XML сохраняется хуком → факты раздела 1 из XML; остальные поля — через LLM; прогон идемпотентен."""
        @__import__("contextlib").asynccontextmanager
        async def fake_db():
            """Подставляет тестовое соединение вместо боевого."""
            yield self.conn

        form = "44fz_competition_obj6"
        fields = [
            ("k_name", 1, "1.1. Наличие информации о наименовании Заказчика", "presence"),
            ("k_kvr", 2, "1.38. Наличие информации о банковском и казначейском сопровождении контракта", "presence"),
            ("k_cmp", 3, "2.1.1. Соответствие наименований", "compliance"),
            ("k_txt", 4, "Комментарий", "text"),
        ]
        self.conn._run(f"DELETE FROM pe_form_fields WHERE form_code = '{form}'")
        for key, n, label, kind in fields:
            self.conn._run(f"INSERT INTO pe_form_fields (form_code, field_key, ordinal, label, value_kind) "
                           f"VALUES ('{form}', '{key}', {n}, '{label}', '{kind}')")
        with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as f:
            f.write(make_xml(version=2).encode("utf-8"))
        try:
            with mock.patch.object(hooks, "_db", fake_db), mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true"}):
                doc = asyncio.run(hooks.on_document_text(7001, "docIzvejenieFiles", "epNotification_1.xml", None, f.name, "flat text"))
                asyncio.run(hooks.on_document_extracted(doc, {"readability": {"status": "allow"}, "raw_data": {}}))
        finally:
            os.unlink(f.name)
        # extraction сохранила и разбор XML, и результат TypeDataExtractor (слияние, а не замена)
        keys = self.q(f"SELECT string_agg(k, ',' ORDER BY k) FROM pe_documents d, jsonb_object_keys(d.extraction) k WHERE d.id={doc}")
        self.assertEqual(keys, "eis_notice,raw_data,readability")

        # текстовый документ с чанком, на который ссылается ответ LLM
        txt = self.conn._run("INSERT INTO pe_documents (expertise_id, doc_code, filename, sha256, text_full) "
                             "VALUES (7001, 'docOpusObjectZacupFiles', 'opisanie.docx', 'abc', 't') RETURNING id").splitlines()[0]
        calls = []

        async def llm(messages, schema):
            """Отвечает цитатой, присутствующей во фрагменте."""
            calls.append(messages)
            return json.dumps({"value": 1, "fragment": 1, "quote": "наименование товара: сервер", "comment": "ок"})

        async def search(conn, expertise_id, vector, k):
            """Возвращает один фрагмент с известным текстом."""
            return [facts.Fragment(1, int(txt), 4, "Описание. Наименование товара: сервер, 2 шт.", "opisanie.docx")]

        ex = facts.FactExtractor(llm, FakeEmbedder(), search=search, system_prompt="SYS")
        stats = asyncio.run(facts.extract_facts(self.conn, 7001, form, ex))
        self.assertEqual(stats["eis"], 1)       # только 1.1; 1.38 XML не решает
        self.assertEqual(stats["fields"], 2)    # k_kvr и k_cmp ушли в LLM, k_name и k_txt — нет
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.q("SELECT fact_key||'|'||source||'|'||(value->>'value') FROM pe_facts WHERE expertise_id=7001 "
                                "AND fact_key='k_name'"), "k_name|eis_xml|1")
        self.assertEqual(self.q("SELECT page||'|'||document_id FROM pe_facts WHERE expertise_id=7001 AND fact_key='k_cmp'"),
                         f"4|{txt}")
        # идемпотентность: повторный прогон не плодит факты
        asyncio.run(facts.extract_facts(self.conn, 7001, form, ex))
        self.assertEqual(self.q("SELECT count(*) FROM pe_facts WHERE expertise_id=7001"), "3")
        # без экстрактора обновляются только XML-факты, ранее найденные факты LLM остаются
        stats = asyncio.run(facts.extract_facts(self.conn, 7001, form, None))
        self.assertEqual((stats["eis"], stats["llm"]), (1, 0))
        self.assertEqual(self.q("SELECT count(*) FROM pe_facts WHERE expertise_id=7001 AND source='fact_extractor'"), "2")

    def test_backfill_process_picks_form_and_handles_unsupported(self):
        """Backfill определяет форму по паспорту, пишет form_code и сообщает о неподдерживаемых формах."""
        from scripts import backfill_facts

        self.conn._run("INSERT INTO pe_form_fields (form_code, field_key, ordinal, label, value_kind) "
                       "VALUES ('44fz_competition_obj6', 'bf_key', 99, '1.1. Наличие информации о наименовании Заказчика', 'presence') "
                       "ON CONFLICT DO NOTHING")
        self.conn._run("INSERT INTO pe_procurements (expertise_id, law, method, object_code, check_type2) VALUES "
                       "(7101, '44-ФЗ', 'Конкурс', 6, 1), (7102, '44-ФЗ', 'Запрос предложений', 6, 9)")
        ok = asyncio.run(backfill_facts.process(self.conn, 7101, None))
        self.assertIn("44fz_competition_obj6", ok)
        self.assertEqual(self.q("SELECT form_code FROM pe_procurements WHERE expertise_id=7101"), "44fz_competition_obj6")
        self.assertIn("не поддерживается", asyncio.run(backfill_facts.process(self.conn, 7102, None)))
        self.assertIn("нет паспорта", asyncio.run(backfill_facts.process(self.conn, 7999, None)))
        self.conn._run("DELETE FROM pe_form_fields WHERE form_code = '44fz_competition_obj6' AND field_key = 'bf_key'")

    def test_build_facts_end_to_end(self):
        """build_facts: форма по паспорту, факты XML + LLM, дайджест с отсутствующими критериями; повтор идемпотентен."""
        @__import__("contextlib").asynccontextmanager
        async def fake_db():
            """Подставляет тестовое соединение вместо боевого."""
            yield self.conn

        form = "44fz_competition_obj6"
        self.conn._run(f"DELETE FROM pe_form_fields WHERE form_code = '{form}'")
        for key, n, label, kind in [("k_name", 1, "1.1. Наличие информации о наименовании Заказчика", "presence"),
                                    ("k_cmp", 2, "2.1.1. Соответствие наименований", "compliance")]:
            self.conn._run(f"INSERT INTO pe_form_fields (form_code, field_key, ordinal, label, value_kind) "
                           f"VALUES ('{form}', '{key}', {n}, '{label}', '{kind}')")
        self.conn._run("INSERT INTO pe_procurements (expertise_id, law, method, object_code, check_type2) "
                       "VALUES (7201, '44-ФЗ', 'Конкурс', 6, 1)")
        self.conn._run("INSERT INTO pe_documents (expertise_id, doc_code, filename, sha256, text_full, extraction) VALUES "
                       "(7201, 'docIzvejenieFiles', 'n.xml', 'x1', 't', "
                       "'{\"eis_notice\": {\"version\": 1, \"criteria\": {\"1.1\": {\"value\": 1, \"evidence\": \"fullName = Вуз\"}}}}')")

        async def fake_index(expertise_id, embedder, **kw):
            """Индексацию в тесте не выполняем (pgvector недоступен)."""
            return {"documents": 0, "chunks": 0, "failed": 0}

        async def fake_search(conn, expertise_id, vector, k):
            """Один фрагмент без нужной цитаты для критерия."""
            return [facts.Fragment(1, 1, 2, "Предмет контракта: услуги связи", "opisanie.docx")]

        async def fake_llm(messages, schema):
            """Модель отвечает «0» без цитаты."""
            return json.dumps({"value": 0, "fragment": 0, "quote": "", "comment": "в описании нет наименований"})

        env = {"KNOWLEDGE_STORE_ENABLED": "true", "PE_FACTS_ENABLED": "true"}
        with mock.patch.dict(os.environ, env), mock.patch.object(hooks, "_db", fake_db), \
                mock.patch.object(indexer, "index_expertise", fake_index), \
                mock.patch.object(indexer, "build_embedder", lambda mgr: FakeEmbedder()), \
                mock.patch.object(facts, "make_llm_call", lambda client, model: fake_llm), \
                mock.patch.object(facts, "search_fragments", fake_search):
            digest = asyncio.run(hooks.build_facts(7201, object(), object(), "model"))
            self.assertEqual(digest["form"], form)
            self.assertEqual((digest["stats"]["checked"], digest["stats"]["present"], digest["stats"]["missing"]), (2, 1, 1))
            self.assertEqual([m["field_key"] for m in digest["missing"]], ["k_cmp"])
            self.assertEqual(digest["missing"][0]["comment"], "в описании нет наименований")
            self.assertEqual(self.q("SELECT form_code FROM pe_procurements WHERE expertise_id=7201"), form)
            again = asyncio.run(hooks.build_facts(7201, object(), object(), "model"))
            self.assertEqual(again["stats"], digest["stats"])
            self.assertEqual(self.q("SELECT count(*) FROM pe_facts WHERE expertise_id=7201"), "2")
            # экспертиза без паспорта и неподдерживаемая форма → None
            self.assertIsNone(asyncio.run(hooks.build_facts(7999, object(), object(), "model")))
        self.conn._run(f"DELETE FROM pe_form_fields WHERE form_code = '{form}'")

    def test_non_xml_file_is_ignored(self):
        """Для не-XML файла (и битого XML) разбор извещения возвращает None."""
        self.assertIsNone(hooks._eis_notice_patch("/nonexistent.txt", "a.txt"))
        with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as f:
            f.write(b"<broken")
        try:
            self.assertIsNone(hooks._eis_notice_patch(f.name, "x.xml"))
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()

"""Связь экспертизы с риск-мониторингом: карточка закупки, продление хранения, находки РМ для эксперта."""
import asyncio
import hashlib
import json
import os
import unittest
from contextlib import asynccontextmanager
from unittest import mock

import pandas as pd

from knowledge_store import documents, hooks, repository as repo, rm_link, runner, summary
from knowledge_store.scope import Scope
from tests import pg_schema
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")


def fake_fetch(purchase=(), contract=()):
    """Запросы к внешней БД-заглушке: карточки по номеру закупки и строки XML по номеру контракта."""
    calls = []

    async def fetch(name, **lists):
        calls.append((name, list(lists.get("nums") or [])))
        return list(purchase) if name == "link_by_number.sql" else list(contract)
    fetch.calls = calls
    return fetch


class FindCardTests(unittest.TestCase):
    """Поиск карточки по номерам."""

    def test_purchase_first(self):
        """Номер закупки находит карточку; пустые номера и дубли отбрасываются."""
        fetch = fake_fetch(purchase=[{"id": 7}, {"id": 9}])
        found = asyncio.run(rm_link.find_card(fetch, ["0373100 ", None, "nan", "0373100"]))
        self.assertEqual(found, {"method": "purchase_number", "rm_card_id": 7, "candidates": [7, 9]})
        self.assertEqual(fetch.calls, [("link_by_number.sql", ["0373100"])])

    def test_contract_fallback_and_none(self):
        """Без карточки по номеру закупки — по номеру контракта (чаще встречающаяся); иначе none."""
        fetch = fake_fetch(contract=[{"risk_monitoring_contract_id": 3, "n": 1}, {"risk_monitoring_contract_id": 4, "n": 5}])
        self.assertEqual(asyncio.run(rm_link.find_card(fetch, ["1771"]))["rm_card_id"], 4)
        self.assertEqual(asyncio.run(rm_link.find_card(fake_fetch(), ["1771"]))["method"], "none")
        self.assertEqual(asyncio.run(rm_link.find_card(fake_fetch(), [None, ""]))["candidates"], [])


class RunnerLinkTests(unittest.TestCase):
    """Сопоставление с РМ после индексации включается флагом и не ломает задачу."""

    def test_flag_and_error(self):
        """Без флага сопоставления нет; ошибка сопоставления попадает в результат."""
        async def fake_index(scope, embedder, **kw):
            return {"documents": 0}

        env = {"KNOWLEDGE_STORE_ENABLED": "true", "PE_FACTS_ENABLED": "false"}
        with mock.patch.object(runner.indexer, "build_embedder", lambda mgr: object()), \
                mock.patch.object(runner.indexer, "index_expertise", fake_index):
            with mock.patch.dict(os.environ, dict(env, PE_RM_LINK_ENABLED="false")):
                self.assertNotIn("rm_link", asyncio.run(runner.index_and_build_facts(Scope("dev", 1), object())))
            with mock.patch.dict(os.environ, dict(env, PE_RM_LINK_ENABLED="true")), \
                    mock.patch.object(runner, "link_rm", mock.AsyncMock(return_value={"rm_card_id": 5})):
                self.assertEqual(asyncio.run(runner.index_and_build_facts(Scope("dev", 1), object()))["rm_link"], {"rm_card_id": 5})
            with mock.patch.dict(os.environ, dict(env, PE_RM_LINK_ENABLED="true")), \
                    mock.patch.object(runner, "link_rm", mock.AsyncMock(side_effect=RuntimeError("x"))):
                self.assertEqual(asyncio.run(runner.index_and_build_facts(Scope("dev", 1), object()))["rm_link"],
                                 {"error": "RuntimeError"})


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class RmLinkDbTests(unittest.TestCase):
    """Идентификаторы, сопоставление и находки на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def _doc(self, name, text="текст"):
        """Общий документ."""
        sha = hashlib.sha256(name.encode()).hexdigest()
        return sha, asyncio.run(documents.save_text(self.conn, sha, None, name, text, documents.READER_FILE, 1))

    def _risk(self, ai_id, risks, created="NOW()", kind="risk_analysis"):
        """Результат риск-анализа РМ в кэше анализов."""
        compact = json.dumps({"risk_score": 60, "risk_level": "medium", "summary": "сводка", "risks": risks},
                             ensure_ascii=False).replace("'", "''")
        self.conn._run(f"INSERT INTO ai_document_analysis (document_id, analysis_type, prompt_version, model, result, compact, created_at) "
                       f"VALUES ({ai_id}, '{kind}', 'v{abs(hash(created)) % 10000}', 'm', '{{}}', '{compact}', {created})")

    def test_identifiers_match_and_findings(self):
        """Номера сохраняются, карточка находится, ссылки РМ продлеваются, находки собираются по двум источникам."""
        c, run, scope = self.conn, asyncio.run, Scope("prod", 8801)

        @asynccontextmanager
        async def fake_db():
            """Тестовое соединение."""
            yield c

        df = pd.DataFrame([{"id": 8801, "law_reference": "44-ФЗ", "procurement_method": "Конкурс", "object": 6,
                            "checkType2": 1, "contract": float("nan")}])
        with mock.patch.object(hooks, "_db", fake_db), mock.patch.dict(os.environ, {"KNOWLEDGE_STORE_ENABLED": "true"}):
            run(hooks.on_run_start(df, "prod"))
            self.assertEqual(c._run("SELECT count(*) FROM procurement_identity WHERE expertise_id = 8801"), "0")  # NaN — не номер
            run(hooks.on_identifiers(scope, "0373100000126000001"))
            run(hooks.on_run_start(df.assign(contract="1771"), "prod"))
        self.assertEqual(c._run("SELECT eis_number||'|'||contract_number FROM procurement_identity WHERE env='prod' AND expertise_id=8801"),
                         "0373100000126000001|1771")
        # документы: один у экспертизы (и в РМ), один только в РМ по той же закупке, один чужой
        sha_e, ai_e = self._doc("notice.pdf")
        _, ai_rm = self._doc("tz.docx")
        _, ai_other = self._doc("other.docx")
        run(repo.upsert_document(c, scope, "docIzvejenieFiles", "notice.pdf", None, sha_e, ai_e, 1, 365))
        c._run(f"INSERT INTO ai_file_links (env, external_file_id, document_id, rm_card_id, expires_at) VALUES "
               f"('prod', 1, {ai_e}, 55, NOW() + interval '10 days'), ('prod', 2, {ai_rm}, 55, NOW() + interval '10 days'), "
               f"('prod', 3, {ai_other}, 56, NOW() + interval '10 days'), ('dev', 4, {ai_rm}, 55, NOW() + interval '10 days')")
        risk = {"code": "DOC-001", "category": "DOC", "severity": "high", "title": "Товарный знак без эквивалента",
                "explanation": "…", "law": "ст. 33", "fragment": "строго марки X", "page": 3, "confidence": 0.9,
                "verification_needed": ["проверить ТЗ"]}
        self._risk(ai_e, [risk], "NOW() - interval '2 days'")
        self._risk(ai_e, [dict(risk, title="новее")], "NOW()", kind="risk_analysis_print_form")
        self._risk(ai_rm, [dict(risk, code="FIN-002", severity="medium")])
        self._risk(ai_other, [risk])
        found = run(rm_link.match_card(c, scope, fake_fetch(purchase=[{"id": 55}])))
        self.assertEqual((found["rm_card_id"], found["method"]), (55, "purchase_number"))
        self.assertEqual(c._run("SELECT count(*) FROM ai_file_links WHERE env='prod' AND rm_card_id=55 "
                                "AND expires_at > NOW() + interval '364 days'"), "2")
        self.assertEqual(c._run("SELECT (expires_at < NOW() + interval '11 days')::text FROM ai_file_links WHERE external_file_id=4"),
                         "true")   # другое окружение не продлевается
        result = run(rm_link.findings(c, scope))
        self.assertEqual(result["rm_card_id"], 55)
        self.assertEqual([d["origin"] for d in result["documents"]], ["expertise", "rm_card"])
        first, second = result["documents"]
        self.assertEqual((first["filename"], first["risks"][0]["title"], first["risks"][0]["page"]), ("notice.pdf", "новее", 3))
        self.assertEqual((second["external_file_ids"], second["risks"][0]["code"]), ([2], "FIN-002"))
        self.assertIn("подсказки", result["note"])
        self.assertEqual(run(summary.rm_findings_safe(c, scope))["rm_card_id"], 55)

    def test_findings_without_identity(self):
        """Без сопоставления — только находки по документам экспертизы; ошибка не мешает сборке."""
        c, run, scope = self.conn, asyncio.run, Scope("dev", 8802)
        sha, ai = self._doc("only.pdf")
        run(repo.upsert_document(c, scope, "docIzvejenieFiles", "only.pdf", None, sha, ai, 1, 365))
        self._risk(ai, [{"code": "DOC-002", "title": "t"}])
        result = run(rm_link.findings(c, scope))
        self.assertIsNone(result["rm_card_id"])
        self.assertEqual(len(result["documents"]), 1)

        class Broken:
            async def fetch(self, *a):
                raise RuntimeError("нет таблицы")
        self.assertEqual(run(summary.rm_findings_safe(Broken(), scope)), {"error": "RuntimeError"})
        self.assertEqual(run(rm_link.match_card(c, Scope("dev", 999999), fake_fetch())), {"skipped": "нет номеров закупки и контракта"})


if __name__ == "__main__":
    unittest.main()

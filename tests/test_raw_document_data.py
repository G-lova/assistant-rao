"""raw_document_data: окружение в ключе (миграция 008), атомарная запись результата, чтение с окружением и без."""
import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from tests import pg_schema
from tests.pg_psql import PsqlConn
from tests.stubs import stubbed_import

PG = os.getenv("PE_TEST_PG")
INIT_SQL = pg_schema.MIGRATIONS.parent / "init.sql"


def _j(value):
    """JSONB как словарь: asyncpg отдаёт строку, тестовое соединение — уже разобранный объект."""
    return json.loads(value) if isinstance(value, str) else value


class ColumnsTests(unittest.TestCase):
    """Колонки по типам документов."""

    def test_document_columns(self):
        """Все колонки перечислены; найденные типы — JSON, остальные — NULL; служебные коды пропускаются."""
        with stubbed_import("configs.working_with_db") as w:
            cols = w.document_columns([{"doc_code": "docIzvejenieFiles", "x": "a\x00b"},
                                       {"doc_code": "linkDocs"}, {"doc_code": "consistency_check"},
                                       {"doc_code": "нет такого"}])
            self.assertEqual(set(cols), set(w.RAW_DOC_COLUMNS))
            self.assertNotIn("summary_report", cols)
            self.assertNotIn("consistency_check", cols)
            self.assertEqual(json.loads(cols["notice"])["x"], "ab")
            self.assertIsNotNone(cols["eis_link"])
            self.assertEqual(sum(v is not None for v in cols.values()), 2)


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class RawDocumentDataDbTests(unittest.TestCase):
    """Запись и чтение на настоящем Postgres."""

    def setUp(self):
        """Тестовая схема (миграция 008 создаёт таблицу) и подмена соединения модуля."""
        host, port = PG.split(":")
        self.conn = PsqlConn(host, int(port))
        pg_schema.build(self.conn)

    def _patched(self, w):
        """Подменяет get_async_db_connection тестовым соединением."""
        conn = self.conn

        @asynccontextmanager
        async def fake():
            """Тестовое соединение."""
            yield conn
        return mock.patch.object(w, "get_async_db_connection", fake)

    def test_upsert_env_and_readers(self):
        """Повторный прогон заменяет строку и сбрасывает пропавшие типы; окружения не пересекаются; чтение без
        окружения берёт самую свежую строку."""
        run, q = asyncio.run, self.conn._run
        with stubbed_import("configs.working_with_db") as w, self._patched(w):
            docs1 = [{"doc_code": "docIzvejenieFiles", "n": 1}, {"doc_code": "docProjContractFiles", "n": 1}]
            summary = {"documents": [{"raw_data": [{"document_name": "Проект контракта", "contract_number": "К-1",
                                                     "finances": [{"value": 100}], "dates": [{"value": "01.10.2026"}]}]}]}
            run(w.save_evaluation_result(6189, "PROD", docs1, summary))
            run(w.save_evaluation_result(6189, "dev", [{"doc_code": "docIzvejenieFiles", "n": 9}], {"s": "dev"}))
            run(w.save_evaluation_result(6189, "prod", [{"doc_code": "docIzvejenieFiles", "n": 2}], summary))
            self.assertEqual(q("SELECT count(*) FROM raw_document_data WHERE procurement_id = 6189"), "2")
            self.assertEqual(q("SELECT (notice->>'n')||'|'||(contract_draft IS NULL)::text FROM raw_document_data "
                               "WHERE env = 'prod' AND procurement_id = 6189"), "2|true")
            self.assertEqual(_j(run(w.get_async_summary_report_from_db(6189, "dev"))), {"s": "dev"})
            info = run(w.get_contract_info_from_db("6189", "prod"))
            self.assertEqual(info, {"contract_number": "К-1", "amount": "100", "date": "01.10.2026"})
            self.assertEqual(run(w.get_contract_info_from_db("6189", "stage")), {"contract_number": "0", "amount": "0", "date": "0"})
            q("UPDATE raw_document_data SET updated_at = NOW() + interval '1 hour' WHERE env = 'dev'")
            with self.assertLogs(w.logger, level="WARNING"):
                self.assertEqual(_j(run(w.get_async_summary_report_from_db(6189))), {"s": "dev"})
            run(w.save_summary_report(7000, {"only": "summary"}, "prod"))
            run(w.save_raw_data(7000, [{"doc_code": "docIzvejenieFiles"}], "prod"))
            self.assertEqual(q("SELECT (summary_report->>'only')||'|'||(notice IS NOT NULL)::text FROM raw_document_data "
                               "WHERE procurement_id = 7000"), "summary|true")
            run(w.delete_procurement_data(6189, "dev"))
            self.assertEqual(q("SELECT string_agg(env, ',') FROM raw_document_data WHERE procurement_id = 6189"), "prod")
            with self.assertRaises(ValueError):
                run(w.save_evaluation_result(1, "prod'; --", [], {}))

    def test_migration_on_legacy_rows(self):
        """Строки до 008 получают PE_LEGACY_ENV, дубли схлопываются до самой свежей, ключ уникален."""
        c = self.conn
        c._run("DROP TABLE IF EXISTS raw_document_data, clean_document_conclusions CASCADE;")
        c._run(INIT_SQL.read_text(encoding="utf-8"))
        c._run("INSERT INTO raw_document_data (procurement_id, summary_report, updated_at) VALUES "
               "(1, '{\"v\": \"old\"}', NOW() - interval '1 day'), (1, '{\"v\": \"new\"}', NOW()), (2, '{\"v\": 2}', NULL)")
        sql = (pg_schema.MIGRATIONS / "008_raw_document_data_env.sql").read_text(encoding="utf-8")
        c._run(pg_schema.shim(sql).replace("'dev'", "'prod'"))
        self.assertEqual(c._run("SELECT string_agg(env || ':' || procurement_id || ':' || (summary_report->>'v'), ',' "
                                "ORDER BY procurement_id) FROM raw_document_data"), "prod:1:new,prod:2:2")
        c._run(pg_schema.shim(sql))     # повторный запуск не падает
        with self.assertRaises(RuntimeError):
            c._run("INSERT INTO raw_document_data (env, procurement_id) VALUES ('prod', 1)")


if __name__ == "__main__":
    unittest.main()

"""Срок хранения ссылок риск-мониторинга: потолок от загрузки, закрытие закупки + 12 месяцев, очистка по живым ссылкам."""
import asyncio
import datetime
import hashlib
import os
import unittest
from unittest import mock

from knowledge_store import repository as repo
from tests import pg_schema
from tests.pg_psql import PsqlConn
from tests.stubs import stubbed_import

PG = os.getenv("PE_TEST_PG")
UTC = datetime.timezone.utc


class EventRetentionTests(unittest.TestCase):
    """Событие XML закрывает или возобновляет срок хранения файлов закупки."""

    def _analyzer(self, module, repo_obj):
        """XmlEventAnalyzer без конструктора (без сети и LLM)."""
        a = module.XmlEventAnalyzer.__new__(module.XmlEventAnalyzer)
        a.repo, a.env = repo_obj, "prod"
        return a

    def test_closing_reopen_and_ignored(self):
        """CON-007 закрывает по дате выгрузки, PUR-009 возобновляет, прочие события и сироты не трогают срок."""
        with stubbed_import("risk_monitoring.xml_event_analyzer") as module:
            fake = mock.MagicMock()
            fake.close_card = mock.AsyncMock(return_value=3)
            fake.reopen_card = mock.AsyncMock(return_value=2)
            a = self._analyzer(module, fake)
            ctx = {"event": {"code": "CON-007"}, "row": {"risk_monitoring_contract_id": 77, "exact_date": "2026-10-01"}}
            self.assertEqual(asyncio.run(a._update_retention(ctx)), {"action": "closed", "card_id": 77, "links": 3})
            self.assertEqual(fake.close_card.call_args.args, ("prod", 77, datetime.datetime(2026, 10, 1, tzinfo=UTC)))
            orphan = {"event": {"code": "PUR-009"}, "row": {}, "link": {"risk_monitoring_contract_id": 5}}
            self.assertEqual(asyncio.run(a._update_retention(orphan))["action"], "reopened")
            self.assertIsNone(asyncio.run(a._update_retention({"event": {"code": "PUR-002"}, "row": {"risk_monitoring_contract_id": 1}})))
            self.assertIsNone(asyncio.run(a._update_retention({"event": {"code": "CON-006"}, "row": {}})))
            fake.close_card.side_effect = RuntimeError("db down")
            self.assertIsNone(asyncio.run(a._update_retention(ctx)))
            self.assertIsNone(asyncio.run(self._analyzer(module, None)._update_retention(ctx)))

    def test_event_date(self):
        """Дата события из date / datetime / строки; мусор — текущий момент."""
        with stubbed_import("risk_monitoring.xml_event_analyzer") as module:
            self.assertEqual(module.event_date(datetime.date(2026, 1, 2)), datetime.datetime(2026, 1, 2, tzinfo=UTC))
            self.assertEqual(module.event_date("2026-01-02 00:00:00").date(), datetime.date(2026, 1, 2))
            self.assertIsNotNone(module.event_date(None).tzinfo)


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class LinkRetentionDbTests(unittest.TestCase):
    """SQL AIRepo для ссылок РМ и очистка по живым ссылкам на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def _repo(self, module):
        """AIRepo поверх тестового соединения."""
        r = module.AIRepo.__new__(module.AIRepo)
        r.pool = self.conn
        return r

    def _doc(self, name: str, text: str) -> int:
        """Общий документ с текстом."""
        sha = hashlib.sha256(name.encode()).hexdigest()
        return int(self.conn._run(f"INSERT INTO ai_documents (content_sha256, text, ocr_status, updated_at) "
                                  f"VALUES ('{sha}', '{text}', 'done', NOW() - interval '2 days') RETURNING id").split()[0])

    def test_link_close_reopen_and_purge(self):
        """Потолок 3 года, закрытие + 365 дней (только сокращает), возобновление, очистка просроченных."""
        run, q = asyncio.run, self.conn._run
        with stubbed_import("configs.ai_repo") as module:
            r = self._repo(module)
            d_open, d_closed = self._doc("open", "активная закупка"), self._doc("closed", "закрытая закупка")
            run(r.link_file("prod", 501, d_open, card_id=10))
            run(r.link_file("prod", 502, d_closed, card_id=20))
            run(r.link_file("dev", 502, d_closed, card_id=20))   # тот же id файла в другом окружении
            self.assertEqual(q("SELECT (expires_at BETWEEN NOW() + interval '1094 days' AND NOW() + interval '1096 days')::text "
                               "FROM ai_file_links WHERE env='prod' AND external_file_id=501"), "true")
            run(r.link_file("prod", 501, d_open, card_id=None))  # повторная связь не теряет карточку и срок
            self.assertEqual(q("SELECT rm_card_id FROM ai_file_links WHERE env='prod' AND external_file_id=501"), "10")
            long_ago = datetime.datetime(2024, 1, 1, tzinfo=UTC)
            self.assertEqual(run(r.close_card("prod", 20, long_ago)), 1)
            self.assertEqual(q("SELECT expires_at::date::text FROM ai_file_links WHERE env='prod' AND external_file_id=502"),
                             "2024-12-31")
            run(r.close_card("prod", 20, datetime.datetime(2026, 1, 1, tzinfo=UTC)))   # повторное закрытие даты не сдвигает
            self.assertEqual(q("SELECT closed_at::date::text FROM ai_file_links WHERE env='prod' AND external_file_id=502"),
                             "2024-01-01")
            # dev-ссылка на тот же документ ещё жива — текст не очищается
            run(repo.purge_expired(self.conn))
            self.assertEqual(q(f"SELECT text FROM ai_documents WHERE id={d_closed}"), "закрытая закупка")
            run(r.close_card("dev", 20, long_ago))
            run(repo.purge_expired(self.conn))
            self.assertEqual(q(f"SELECT (text IS NULL)::text FROM ai_documents WHERE id={d_closed}"), "true")
            self.assertEqual(q(f"SELECT text FROM ai_documents WHERE id={d_open}"), "активная закупка")
            doc = run(r.get_document(d_closed))
            self.assertIsNotNone(doc["text_purged_at"])
            # возобновление снимает закрытие; legal_hold держит текст даже после срока
            self.assertEqual(run(r.reopen_card("prod", 20)), 1)
            self.assertEqual(q("SELECT (closed_at IS NULL AND expires_at > NOW())::text FROM ai_file_links "
                               "WHERE env='prod' AND external_file_id=502"), "true")
            d_hold = self._doc("hold", "разбор")
            run(r.link_file("prod", 503, d_hold, card_id=30))
            q("UPDATE ai_file_links SET expires_at = NOW() - interval '1 day', legal_hold = TRUE WHERE external_file_id=503")
            run(repo.purge_expired(self.conn))
            self.assertEqual(q(f"SELECT text FROM ai_documents WHERE id={d_hold}"), "разбор")

    def test_upsert_resets_purge_mark(self):
        """Повторное чтение очищенного документа РМ возвращает текст и снимает отметку очистки."""
        with stubbed_import("configs.ai_repo") as module:
            r = self._repo(module)
            sha = hashlib.sha256(b"repurged").hexdigest()
            self.conn._run(f"INSERT INTO ai_documents (content_sha256, text, ocr_status, text_purged_at) VALUES ('{sha}', NULL, 'done', NOW())")
            asyncio.run(r.upsert_document(sha, "t", ".pdf", 1, "снова", reader="rm_pdf_text"))
            self.assertEqual(self.conn._run(f"SELECT text||'|'||reader||'|'||(text_purged_at IS NULL)::text FROM ai_documents "
                                            f"WHERE content_sha256='{sha}'"), "снова|rm_pdf_text|true")


if __name__ == "__main__":
    unittest.main()

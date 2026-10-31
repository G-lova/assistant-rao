"""Тесты публикаций экспертов: ссылки, авторство, снимок и синхронизация, разбор, similarity (search_experts.publications).

Интеграционные тесты синхронизации, очереди и similarity выполняются на настоящем Postgres (``PE_TEST_PG=host:port``).
"""
import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager
from unittest import mock

import pandas as pd

from configs import settings
from search_experts.publications import extract, repo, similarity, sync
from search_experts.publications.authors import author_matches, surname_of
from search_experts.publications.links import classify, extract_doi, normalize_url, parse_link_list
from tests import pg_schema
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")
LINK_TYPES = ("doi", "file", "cloud", "journal")
MEDIA, STORAGE = "https://portal/api/media/", "https://portal/storage/"


def run(coro):
    """Выполняет корутину в новом цикле событий."""
    return asyncio.run(coro)


class LinksTest(unittest.TestCase):
    """Нормализация и классификация ссылок анкеты (примеры из users.csv)."""

    def test_parse_link_list(self):
        """JSON-массив, ``[null]``, пустые значения и строка без JSON."""
        self.assertEqual(parse_link_list('["https://a.ru/1", null, ""]'), ["https://a.ru/1"])
        self.assertEqual(parse_link_list("[null]"), [])
        self.assertEqual(parse_link_list(None), [])
        self.assertEqual(parse_link_list("https://a.ru"), ["https://a.ru"])

    def test_normalize(self):
        """Префикс PDF-просмотрщика Chrome, DOI как домен, адрес без схемы."""
        self.assertEqual(normalize_url("https://efaidnbmnnnibpcajpcglclefindmkaj/https://www.elibrary.ru/download/x.pdf"),
                         "https://www.elibrary.ru/download/x.pdf")
        self.assertEqual(normalize_url("https://10.26617/1810-3111-2020-3(108)-11-16"),
                         "https://doi.org/10.26617/1810-3111-2020-3(108)-11-16")
        self.assertEqual(normalize_url("10.1007/s10517-022-05542-y"), "https://doi.org/10.1007/s10517-022-05542-y")
        self.assertEqual(normalize_url("www.cyberleninka.ru/article/n/x"), "https://www.cyberleninka.ru/article/n/x")
        self.assertIsNone(normalize_url("статья в журнале"))

    def test_classify_and_doi(self):
        """DOI из doi.org, издательства и Scopus; eLibrary, профили, файлы, облака, библиотеки, журналы."""
        cases = {
            "https://doi.org/10.3390/ijms23169193": "doi",
            "https://link.springer.com/article/10.1007/s10517-022-05542-y": "doi",
            "https://www.scopus.com/inward/record.uri?eid=2-s2.0-85133519251&doi=10.24234%2fwisdom.v2i1.762&partnerID=40": "doi",
            "https://www.scopus.com/authid/detail.uri?authorId=56014957500": "scopus",
            "https://www.elibrary.ru/item.asp?id=49295077": "elibrary",
            "https://elibrary.ru/yuiczn": "elibrary",
            "https://www.researchgate.net/profile/Kh-Makhmudov-2": "profile",
            "https://svpin.org/files/journal/2023/1/n118-p96.pdf": "file",
            "https://disk.yandex.ru/d/sXxvl6WEBnihTw": "cloud",
            "https://search.rsl.ru/ru/record/01011030277": "library",
            "https://cyberleninka.ru/article/n/rol-t-kletok": "journal",
        }
        for url, kind in cases.items():
            self.assertEqual(classify(url), kind, url)
        self.assertEqual(extract_doi("https://www.scopus.com/inward/record.uri?doi=10.24234%2fwisdom.v2i1.762&x=1"),
                         "10.24234/wisdom.v2i1.762")
        self.assertEqual(extract_doi("https://link.springer.com/content/pdf/10.1007/s10517-022-05542-y.pdf"),
                         "10.1007/s10517-022-05542-y")


class AuthorsTest(unittest.TestCase):
    """Проверка авторства."""

    def test_author_matches(self):
        """Кириллица, латиница (транслитерация), падеж, чужая статья, авторы не указаны."""
        name = "Жанаева Светлана Яковлевна"
        self.assertEqual(surname_of(name), "жанаева")
        self.assertTrue(author_matches(name, ["Идова Г.В.", "Альперина Е.Л.", "Жанаева С.Я."]))
        self.assertTrue(author_matches(name, ["S.Ya. Zhanaeva", "G.V. Idova"]))
        self.assertTrue(author_matches(name, ["Жанаевой С. Я."]))
        self.assertFalse(author_matches(name, ["Петров А.А.", "Сидорова Б.Б."]))
        self.assertIsNone(author_matches(name, None))
        self.assertIsNone(author_matches(None, ["Жанаева С.Я."]))


class SnapshotTest(unittest.TestCase):
    """Снимок экспертов из строк внешней БД."""

    USERS = [
        {"expert_id": 13, "name": "Жанаева Светлана Яковлевна", "deleted_at": None,
         "linkPublication": '["https://doi.org/10.3390/ijms23169193", "https://www.elibrary.ru/item.asp?id=1"]',
         "linkMonographs": '["https://doi.org/10.3390/ijms23169193"]'},
        {"expert_id": 14, "name": "Иванов И.И.", "deleted_at": "2026-10-01", "linkPublication": "[]", "linkMonographs": None},
    ]
    MEDIA_ROWS = [
        {"media_id": 5444, "expert_id": 13, "collection_name": "documentPublication",
         "file_name": "elibrary_53772365_80401437.pdf", "disk": "public"},
        {"media_id": 9, "expert_id": 999, "collection_name": "documentMonographs", "file_name": "x.pdf", "disk": "media"},
    ]

    def test_build_snapshot(self):
        """Ссылка в двух полях — один источник; файл не-эксперта не учитывается; адрес файла по disk."""
        snap = sync.build_snapshot(self.USERS, self.MEDIA_ROWS, MEDIA, STORAGE)
        self.assertEqual(set(snap), {13, 14})
        s13 = snap[13]
        self.assertEqual([s.link_type for s in s13.sources], ["doi", "elibrary", "media"])
        self.assertEqual(s13.sources[2].url, "https://portal/storage/5444/elibrary_53772365_80401437.pdf")
        self.assertTrue(snap[14].deleted)
        self.assertEqual(sync.media_url(9, "x.pdf", "media", MEDIA, STORAGE), "https://portal/api/media/9/x.pdf")

    def test_hash_order_independent(self):
        """Отпечаток не зависит от порядка источников и меняется при новом источнике."""
        snap = sync.build_snapshot(self.USERS, self.MEDIA_ROWS, MEDIA, STORAGE)[13]
        h = snap.sources_hash()
        snap.sources.reverse()
        self.assertEqual(snap.sources_hash(), h)
        snap.sources.append(sync.Source("link", "https://x.ru", "publication", "https://x.ru", None, "journal"))
        self.assertNotEqual(snap.sources_hash(), h)

    def test_filter_sql(self):
        """Фильтр по экспертам подставляется плейсхолдерами портала."""
        sql, b = sync._filter_sql("SELECT 1 FROM users u WHERE 1 {{EXPERT_FILTER}}", "u.id", [1, 2])
        self.assertIn("AND u.id IN (?, ?)", sql)
        self.assertEqual(b, [1, 2])
        sql, b = sync._filter_sql("WHERE 1 {{EXPERT_FILTER}}", "u.id", None)
        self.assertEqual((sql.strip(), b), ("WHERE 1", []))


class FakeLLM:
    """Модель-заглушка: отвечает по очереди заготовленными ответами и запоминает запросы."""

    model = "fake"

    def __init__(self, answers):
        """Запоминает ответы."""
        self.answers = list(answers)
        self.calls = []

    async def ask(self, system, user, schema=None, max_tokens=0, list_key=None, **kw):
        """Возвращает следующий ответ."""
        self.calls.append(user)
        return self.answers.pop(0)


class ExtractTest(unittest.TestCase):
    """Разбор источников."""

    def test_normalize_publication(self):
        """Год из даты, авторы строкой, неизвестный тип, короткое название, DOI из ссылки."""
        p = extract.normalize_publication({"title": " Роль Т-клеток ", "year": "2023-05-01", "authors": "Идова Г.В., Жанаева С.Я.",
                                           "pub_type": "review", "doi": "https://doi.org/10.26617/X-1"})
        self.assertEqual((p["title"], p["year"], p["authors"], p["pub_type"], p["doi"]),
                         ("Роль Т-клеток", 2023, ["Идова Г.В.", "Жанаева С.Я."], "other", "10.26617/x-1"))
        self.assertIsNone(extract.normalize_publication({"title": "abc"}))
        self.assertIsNone(extract.normalize_publication({"title": "Название", "year": 1820})["year"])

    def test_dedupe_and_split(self):
        """Повторы по названию сливаются; длинный текст режется на части."""
        a = extract.normalize_publication({"title": "Статья о клетках", "year": 2020})
        b = extract.normalize_publication({"title": "статья  о клетках.", "venue": "Журнал"})
        out = extract.dedupe([a, b], 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["venue"], "Журнал")
        parts = extract.split_text("\n\n".join(f"{i}. Публикация номер {i}" * 20 for i in range(50)), 2000, 4)
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(len(p) <= 2000 for p in parts))

    def test_openalex_and_crossref(self):
        """Аннотация из инвертированного индекса, авторы, журнал, тип."""
        work = {"title": "T-cells in Parkinson disease", "publication_year": 2023, "type": "article",
                "doi": "https://doi.org/10.3389/fnins.2023.1217702",
                "authorships": [{"author": {"display_name": "Svetlana Zhanaeva"}}],
                "primary_location": {"source": {"display_name": "Frontiers in Neuroscience"}},
                "abstract_inverted_index": {"T-cells": [0], "drive": [1], "neuroinflammation": [2]},
                "keywords": [{"display_name": "Neuroinflammation"}]}
        p = extract.from_openalex(work)
        self.assertEqual(p["abstract"], "T-cells drive neuroinflammation")
        self.assertEqual((p["venue"], p["doi"], p["keywords"]), ("Frontiers in Neuroscience", "10.3389/fnins.2023.1217702", "Neuroinflammation"))
        c = extract.from_crossref({"title": ["Книга"], "type": "monograph", "author": [{"family": "Иванов", "given": "И."}],
                                   "issued": {"date-parts": [[2019, 1]]}, "abstract": "<jats:p>Текст</jats:p>"})
        self.assertEqual((c["pub_type"], c["year"], c["authors"], c["abstract"]), ("monograph", 2019, ["Иванов И."], "Текст"))

    def test_html_meta(self):
        """citation_* со страницы журнала; страница без названия статьи — ничего."""
        page = """<html><head><meta name="citation_title" content="Роль Т-клеточных субпопуляций">
        <meta name="citation_author" content="Идова Г.В."><meta name="citation_author" content="Жанаева С.Я.">
        <meta name="citation_publication_date" content="2023/03/30"><meta name="citation_journal_title" content="Сибирский вестник">
        <meta name="citation_keywords" content="болезнь Паркинсона"><meta name="citation_doi" content="10.26617/1810-3111-2023-1(118)-96-103">
        <meta name="description" content="Описание журнала"></head></html>"""
        pub, _ = extract.from_html_meta(page)
        self.assertEqual(pub["authors"], ["Идова Г.В.", "Жанаева С.Я."])
        self.assertEqual((pub["year"], pub["venue"], pub["keywords"]), (2023, "Сибирский вестник", "болезнь Паркинсона"))
        self.assertIsNone(pub["abstract"])
        self.assertEqual(extract.from_html_meta("<meta name='description' content='x'>"), (None, None))

    def test_parse_text_with_llm(self):
        """Статья — одна запись; список трудов — все части; прочее — пусто."""
        llm = FakeLLM([{"doc_kind": "article", "publications": [{"title": "Роль Т-клеток", "authors": ["Жанаева С.Я."]},
                                                                 {"title": "Лишняя запись из списка литературы"}]}])
        r = run(extract.parse_text_with_llm(llm, "текст статьи", "a.pdf", "publication", 1000, 5, 100))
        self.assertEqual([p["title"] for p in r["publications"]], ["Роль Т-клеток"])

        text = "\n\n".join(f"{i}. Название работы номер {i}, журнал, {2000 + i % 20}" for i in range(120))
        answers = [{"doc_kind": "list", "publications": [{"title": f"Работа части {k}", "pub_type": "article"}]} for k in range(10)]
        llm = FakeLLM(answers)
        r = run(extract.parse_text_with_llm(llm, text, "список.docx", "publication", 1500, 3, 100))
        self.assertEqual(r["doc_kind"], "list")
        self.assertEqual(r["parts"], 3)
        self.assertEqual(len(r["publications"]), 3)

        llm = FakeLLM([{"doc_kind": "other", "publications": [{"title": "Диплом кандидата наук"}]}])
        self.assertEqual(run(extract.parse_text_with_llm(llm, "диплом", "d.pdf", "publication", 1000, 3, 100))["publications"], [])

    def test_embed_text(self):
        """Текст эмбеддинга — название, ключевые слова, аннотация, журнал."""
        self.assertEqual(extract.embed_text({"title": "Название.", "keywords": "к1, к2", "abstract": None, "venue": "Журнал"}),
                         "Название. к1, к2. Журнал")


class SimilarityTest(unittest.TestCase):
    """Агрегирование сходства публикаций и смешивание со сходством анкеты."""

    def test_aggregate(self):
        """Среднее K лучших с учётом свежести; год неизвестен — нижняя граница."""
        rows = [{"expert_id": 1, "similarity": 0.8, "year": 2026, "title": "A"},
                {"expert_id": 1, "similarity": 0.6, "year": 2012, "title": "B"},
                {"expert_id": 1, "similarity": 0.1, "year": 2026, "title": "C"},
                {"expert_id": 2, "similarity": 0.9, "year": None, "title": "D"}]
        out = similarity.aggregate(rows, top_k=2, half_life=7, floor=0.5, current_year=2026)
        self.assertAlmostEqual(out[1]["score"], (0.8 + 0.6 * 0.5) / 2)   # 2012: 0.5 ** 2 = 0.25 → нижняя граница 0.5
        self.assertEqual((out[1]["count"], out[1]["top_title"]), (3, "A"))
        self.assertAlmostEqual(out[2]["score"], 0.45)

    def test_blend(self):
        """У эксперта с публикациями сходство смешивается, без публикаций — прежнее."""
        df = pd.DataFrame({"expert_id": [1, 2], "similarity_embeddings": [0.5, 0.7]})
        df = similarity.blend(df, {1: {"score": 0.9, "count": 4, "top_title": "A", "top_year": 2024}}, 0.4)
        self.assertAlmostEqual(df.loc[0, "similarity_embeddings"], 0.6 * 0.5 + 0.4 * 0.9)
        self.assertAlmostEqual(df.loc[1, "similarity_embeddings"], 0.7)
        self.assertTrue(pd.isna(df.loc[1, "similarity_publications"]))
        self.assertEqual(list(df["publications_count"]), [4, 0])


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class SyncDbTest(unittest.TestCase):
    """Синхронизация, очередь, результат разбора и запрос similarity на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему (все миграции)."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def setUp(self):
        """Очищает таблицы публикаций."""
        self.conn._run("TRUNCATE expert_publications, expert_pub_sources, expert_pub_experts RESTART IDENTITY CASCADE")

    def _snap(self, users, media=()):
        """Снимок из строк."""
        return sync.build_snapshot(users, list(media), MEDIA, STORAGE)

    def _apply(self, snap, full=True):
        """Применяет снимок."""
        return run(sync.apply_snapshot(self.conn, "dev", snap, full, LINK_TYPES))

    def _sources(self):
        """Источники: ключ → статус."""
        rows = run(self.conn.fetch("SELECT source_kind, source_key, status FROM expert_pub_sources ORDER BY id"))
        return {f"{r['source_kind']}:{r['source_key']}": r["status"] for r in rows}

    def _user(self, eid=13, name="Жанаева Светлана Яковлевна", links=("https://doi.org/10.3390/ijms23169193",
                                                                     "https://www.elibrary.ru/item.asp?id=1"), deleted=None):
        """Строка эксперта."""
        return {"expert_id": eid, "name": name, "deleted_at": deleted, "linkPublication": json.dumps(list(links)),
                "linkMonographs": "[null]"}

    def test_lifecycle(self):
        """Новый эксперт → изменения анкеты → смена ФИО → удаление → возврат."""
        media = [{"media_id": 5444, "expert_id": 13, "collection_name": "documentPublication", "file_name": "a.pdf", "disk": "public"}]
        st = self._apply(self._snap([self._user()], media))
        self.assertEqual(st["experts_new"], 1)
        self.assertEqual(self._sources(), {"link:https://doi.org/10.3390/ijms23169193": "pending",
                                           "link:https://www.elibrary.ru/item.asp?id=1": "unsupported",
                                           "media:5444": "pending"})
        # повторный запуск без изменений: эксперт пропускается
        st = self._apply(self._snap([self._user()], media))
        self.assertEqual((st["experts_unchanged"], st["sources_upserted"]), (1, 0))

        # загружен новый файл, удалена ссылка DOI
        media2 = media + [{"media_id": 6000, "expert_id": 13, "collection_name": "documentMonographs", "file_name": "b.pdf", "disk": "media"}]
        st = self._apply(self._snap([self._user(links=("https://www.elibrary.ru/item.asp?id=1",))], media2))
        self.assertEqual((st["experts_changed"], st["sources_removed"]), (1, 1))
        src = self._sources()
        self.assertEqual(src["link:https://doi.org/10.3390/ijms23169193"], "removed")
        self.assertEqual(src["media:6000"], "pending")

        # ссылка вернулась — снова в очередь
        self._apply(self._snap([self._user(links=("https://doi.org/10.3390/ijms23169193",))], media2))
        self.assertEqual(self._sources()["link:https://doi.org/10.3390/ijms23169193"], "pending")

        # эксперт удалён во внешней БД
        st = self._apply(self._snap([self._user(deleted="2026-10-30")], media2))
        self.assertEqual(st["experts_deleted"], 1)
        self.assertEqual(set(self._sources().values()), {"removed"})

        # восстановлен
        st = self._apply(self._snap([self._user()], media2))
        self.assertEqual(st["experts_changed"], 1)
        self.assertIn("pending", self._sources().values())

    def test_full_sync_missing_expert_and_partial(self):
        """Полная синхронизация: эксперта нет в выборке — удалён; частичная не трогает остальных."""
        self._apply(self._snap([self._user(13), self._user(14, "Иванов Иван")]))
        st = self._apply(self._snap([self._user(13)]), full=False)
        self.assertEqual(st["experts_deleted"], 0)
        st = self._apply(self._snap([self._user(13)]), full=True)
        self.assertEqual(st["experts_deleted"], 1)
        rows = run(self.conn.fetch("SELECT status FROM expert_pub_sources WHERE expert_id = 14"))
        self.assertEqual({r["status"] for r in rows}, {"removed"})

    def test_queue_result_rename_similarity(self):
        """Очередь → результат разбора → эмбеддинги → similarity; смена ФИО перепроверяет авторство."""
        self._apply(self._snap([self._user()]))
        claimed = run(repo.claim_sources(self.conn, "dev", 10, 12, 3))
        self.assertEqual(len(claimed), 1)                      # eLibrary не берётся
        self.assertEqual(claimed[0]["expert_name"], "Жанаева Светлана Яковлевна")
        self.assertEqual(run(repo.claim_sources(self.conn, "dev", 10, 12, 3)), [])   # уже в работе

        src = claimed[0]
        pubs = [dict(extract.normalize_publication({"title": "Своя статья", "year": 2025, "authors": ["Жанаева С.Я."]}), author_verified=True),
                dict(extract.normalize_publication({"title": "Чужая статья", "year": 2025, "authors": ["Петров П.П."]}), author_verified=False)]
        ids = run(repo.save_source_result(self.conn, src, pubs, None))
        self.assertEqual(len(ids), 2)
        todo = run(repo.publications_without_embedding(self.conn, "dev", "m1", 100))
        self.assertEqual(len(todo), 2)
        run(repo.save_embeddings(self.conn, [(ids[0], "s1", [1.0, 0.0, 0.0]), (ids[1], "s2", [0.0, 1.0, 0.0])], "m1"))

        rows = run(repo.publication_similarities(self.conn, "dev", [13, 99], [1.0, 0.0, 0.0], "m1", True))
        self.assertEqual([(r["title"], round(float(r["similarity"]), 3)) for r in rows], [("Своя статья", 1.0)])
        rows = run(repo.publication_similarities(self.conn, "dev", [13], [1.0, 0.0, 0.0], "m1", False))
        self.assertEqual(len(rows), 2)

        # фамилия сменилась (Петров) — авторство пересчитано без повторного разбора
        st = self._apply(self._snap([self._user(name="Петров Пётр Петрович")]))
        self.assertEqual(st["renamed"], 1)
        rows = run(self.conn.fetch("SELECT title, author_verified FROM expert_publications ORDER BY pub_no"))
        self.assertEqual([r["author_verified"] for r in rows], [False, True])

        # источник удалён из анкеты — публикации в similarity не участвуют
        self._apply(self._snap([self._user(name="Петров Пётр Петрович", links=())]))
        self.assertEqual(run(repo.publication_similarities(self.conn, "dev", [13], [1.0, 0.0, 0.0], "m1", False)), [])
        stats = run(repo.stats(self.conn, "dev"))
        self.assertEqual(stats["publications"], 0)

    def test_process_batch_with_fake_sources(self):
        """Обработка: успешный источник, неудачный (failed, повтор позже), освобождение по времени."""
        from search_experts.publications.processor import PublicationProcessor

        users = [self._user(13, links=("https://doi.org/10.1234/a", "https://doi.org/10.1234/b"))]
        self._apply(self._snap(users))
        conn = self.conn

        @asynccontextmanager
        async def connection():
            yield conn

        proc = PublicationProcessor("dev", http_manager=None, connection=connection)

        async def fake_doi(doi):
            if doi.endswith("/b"):
                raise RuntimeError("OpenAlex 503")
            return [extract.normalize_publication({"title": "Статья по DOI", "authors": ["Zhanaeva S."], "year": 2024})]

        async def no_embed(source_ids=None, limit=0):
            return 0

        with mock.patch.object(proc, "from_doi", side_effect=fake_doi), mock.patch.object(proc, "embed_publications", side_effect=no_embed):
            st = run(proc.process_batch(limit=10, budget_sec=100))
        self.assertEqual((st["claimed"], st["done"], st["failed"]), (2, 1, 1))
        rows = run(conn.fetch("SELECT source_key, status, error FROM expert_pub_sources ORDER BY id"))
        self.assertEqual([r["status"] for r in rows], ["done", "failed"])
        self.assertIn("OpenAlex 503", rows[1]["error"])
        pub = run(conn.fetchrow("SELECT author_verified FROM expert_publications"))
        self.assertTrue(pub["author_verified"])
        # неудачный повторяется только через EXPERT_PUB_RETRY_HOURS
        self.assertEqual(run(repo.claim_sources(conn, "dev", 10, 12, 3)), [])
        self.assertEqual(len(run(repo.claim_sources(conn, "dev", 10, 0, 3))), 1)


if __name__ == "__main__":
    unittest.main()


class PipelineIntegrationTest(unittest.TestCase):
    """Сходство публикаций в ScoringPipeline: смешивание, отказ внутренней БД, выключатель."""

    def _pipeline(self, module):
        """ScoringPipeline без конструктора (без конфигурации и HTTP)."""
        p = module.ScoringPipeline.__new__(module.ScoringPipeline)
        p.environment, p.embedding_model = "dev", "m1"
        return p

    def test_apply_publication_similarity(self):
        """Оценки публикаций смешиваются; ошибка БД и выключенный учёт оставляют сходство анкеты."""
        from tests.stubs import stubbed_import

        with stubbed_import("search_experts.pipeline") as module:
            p = self._pipeline(module)
            df = pd.DataFrame({"expert_id": [1, 2], "similarity_embeddings": [0.5, 0.7]})

            async def scores(env, ids, emb, model, connection=None):
                self.assertEqual((env, sorted(ids), model), ("dev", [1, 2], "m1"))
                return {1: {"score": 1.0, "count": 2, "top_title": "A", "top_year": 2025}}

            # внутри stubbed_import модули проекта импортированы заново — подменяем по пути
            with mock.patch("search_experts.publications.similarity.publication_scores", side_effect=scores), \
                    mock.patch("configs.settings.EXPERT_PUB_SIMILARITY_WEIGHT", 0.5):
                out = run(p.apply_publication_similarity(df.copy(), [0.1, 0.2]))
            self.assertEqual(list(out["similarity_embeddings"].round(3)), [0.75, 0.7])

            async def broken(*a, **k):
                raise ConnectionError("нет БД")

            with mock.patch("search_experts.publications.similarity.publication_scores", side_effect=broken):
                out = run(p.apply_publication_similarity(df.copy(), [0.1]))
            self.assertEqual(list(out["similarity_embeddings"]), [0.5, 0.7])
            with mock.patch("configs.settings.EXPERT_PUB_SIMILARITY_ENABLED", False):
                out = run(p.apply_publication_similarity(df.copy(), [0.1]))
            self.assertNotIn("similarity_publications", out.columns)

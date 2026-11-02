"""Тесты соавторства в конфликте интересов (search_experts.coauthors).

Запрос авторов публикаций проверяется на настоящем Postgres (``PE_TEST_PG=host:port``).
"""
import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager
from unittest import mock

import pandas as pd

from search_experts import coauthors
from search_experts.coauthors import Person, parse_person, same_person
from tests import pg_schema
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")


class PersonTest(unittest.TestCase):
    """Разбор ФИО и сравнение людей."""

    def test_parse(self):
        """Все записи ФИО; организация и фамилия без инициалов — None."""
        self.assertEqual(parse_person("Жанаева С.Я."), Person("жанаева", ("с", "я")))
        self.assertEqual(parse_person("С.Я. Жанаева"), Person("жанаева", ("с", "я")))
        self.assertEqual(parse_person("Жанаева Светлана Яковлевна"), Person("жанаева", ("с", "я")))
        self.assertEqual(parse_person("S.Ya. Zhanaeva"), Person("zhanaeva", ("s", "ya")))
        self.assertEqual(parse_person("Ivanov, I."), Person("ivanov", ("i",)))
        self.assertEqual(parse_person("маликов а.а."), Person("маликов", ("а", "а")))
        self.assertEqual(parse_person("Иванов И.И.Петров"), None)   # склейка двух авторов не разбирается
        self.assertIsNone(parse_person("Иванов"))
        self.assertIsNone(parse_person("ФГБОУ ВО Тульский государственный университет имени Л.Н. Толстого"))

    def test_parse_people(self):
        """Несколько контактных лиц через запятую; «Ivanov, I.» — один человек."""
        people = coauthors.parse_people("Шамшурин Виталий Викторович, Толмачева Эльвира Анатольевна")
        self.assertEqual([p.surname for p in people], ["шамшурин", "толмачева"])
        self.assertEqual(coauthors.parse_people("Ivanov, I."), [Person("ivanov", ("i",))])
        self.assertEqual(coauthors.parse_people("Московский государственный университет геодезии и картографии"), [])

    def test_same_person(self):
        """Кириллица ↔ латиница; разные инициалы, мужская/женская фамилия, неполные инициалы."""
        z = parse_person("Жанаева Светлана Яковлевна")
        self.assertTrue(same_person(z, parse_person("S.Ya. Zhanaeva")))
        self.assertTrue(same_person(z, parse_person("Жанаева С.")))
        self.assertFalse(same_person(z, parse_person("Жанаева С.П.")))
        self.assertFalse(same_person(parse_person("Иванов И.И."), parse_person("Иванова И.И.")))
        self.assertFalse(same_person(parse_person("Иванов И.И."), parse_person("Иванов П.И.")))
        self.assertFalse(same_person(z, None))

    def test_coauthors_of(self):
        """Сам эксперт из соавторов исключается."""
        out = coauthors.coauthors_of("Жанаева Светлана Яковлевна", ["Идова Г.В.", "Альперина Е.Л.", "Жанаева С.Я."])
        self.assertEqual([p.surname for p in out], ["идова", "альперина"])


PUBS = {
    13: [("Роль Т-клеток", 2023, ["Идова Г.В.", "Маликов А.А.", "Жанаева С.Я."])],
    20: [("Другая статья", 2024, ["Петров П.П.", "Zhanaeva S.Ya."])],
    30: [("Чужое", 2022, ["Сидоров С.С."])],
}


def frame():
    """Кандидаты одной экспертизы: заказчик — контактное лицо Маликов А.А., руководитель Кравченко О.А."""
    return pd.DataFrame({
        "expert_id": [13, 20, 30],
        "expert_name": ["жанаева светлана яковлевна", "петров пётр петрович", "сидоров семён семёнович"],
        "expertise_name": ["маликов а.а."] * 3,
        "expertise_director_name": ["КРАВЧЕНКО ОЛЕГ АЛЕКСАНДРОВИЧ"] * 3,
        "similarity_embeddings": [0.6, 0.8, 0.5],
    })


class ChecksTest(unittest.TestCase):
    """Соавторство с заказчиком и между кандидатами."""

    def test_customer(self):
        """Маликов А.А. — соавтор эксперта 13 → конфликт; у остальных нет."""
        out = coauthors.customer_coauthor_conflicts(frame(), PUBS)
        self.assertEqual(list(out), [13])
        self.assertIn("Роль Т-клеток", out[13])

    def test_director(self):
        """Руководитель заказчика среди соавторов — тоже конфликт."""
        pubs = {30: [("Статья", 2025, ["Кравченко О.А.", "Сидоров С.С."])]}
        self.assertEqual(list(coauthors.customer_coauthor_conflicts(frame(), pubs)), [30])

    def test_expert_pairs(self):
        """13 и 20 — соавторы (латиница); исключается 13 (сходство ниже)."""
        pairs = coauthors.expert_coauthor_pairs(frame(), PUBS)
        self.assertEqual([(a, b) for a, b, _ in pairs], [(20, 13)] if pairs[0][0] == 20 else [(13, 20)])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(coauthors.exclude_coauthor_pairs(frame(), pairs), {13})

    def test_no_customer_names(self):
        """Нет ФИО заказчика — проверка не выполняется."""
        df = frame()
        df["expertise_name"], df["expertise_director_name"] = None, None
        self.assertEqual(coauthors.customer_coauthor_conflicts(df, PUBS), {})


class PipelineTest(unittest.TestCase):
    """Проверки в ScoringPipeline: detect_conflicts и detect_nepotism."""

    def test_pipeline(self):
        """Соавтор заказчика исключается до сходства, пара кандидатов — после; без публикаций — как раньше."""
        from tests.stubs import stubbed_import

        with stubbed_import("search_experts.pipeline") as module:
            p = module.ScoringPipeline.__new__(module.ScoringPipeline)
            p.conflict_detector = mock.Mock()
            p.conflict_detector.find_requisite_conflicts.return_value = {}
            p.conflict_detector.find_family_conflicts.return_value = set()
            p.conflict_detector.find_nepotism.return_value = set()
            p.coauthor_publications = PUBS
            self.assertEqual(sorted(p.detect_conflicts(frame())["expert_id"]), [20, 30])
            self.assertEqual(sorted(p.detect_nepotism(frame())["expert_id"]), [20, 30])
            p.coauthor_publications = {}
            self.assertEqual(sorted(p.detect_conflicts(frame())["expert_id"]), [13, 20, 30])


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class LoadDbTest(unittest.TestCase):
    """Загрузка авторов: только done-источники, без чужих статей, с ограничением по году."""

    @classmethod
    def setUpClass(cls):
        """Тестовая схема и публикации."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)
        c = cls.conn
        c._run("INSERT INTO expert_pub_sources (id, env, expert_id, source_kind, source_key, collection, status) VALUES "
               "(1, 'dev', 13, 'media', '1', 'publication', 'done'), (2, 'dev', 13, 'media', '2', 'publication', 'removed')")
        rows = [(1, 0, "Своя новая", 2025, ["Маликов А.А.", "Жанаева С.Я."], "true"),
                (1, 1, "Своя старая", 2010, ["Маликов А.А.", "Жанаева С.Я."], "true"),
                (1, 2, "Чужая", 2025, ["Маликов А.А."], "false"),
                (1, 3, "Без года", None, ["Маликов А.А.", "Жанаева С.Я."], "null"),
                (2, 0, "Удалённый источник", 2025, ["Маликов А.А.", "Жанаева С.Я."], "true")]
        for src, no, title, year, authors, verified in rows:
            c._run("INSERT INTO expert_publications (source_id, env, expert_id, pub_no, title, year, authors, author_verified) "
                   f"VALUES ({src}, 'dev', 13, {no}, '{title}', {year or 'NULL'}, '{json.dumps(authors, ensure_ascii=False)}', {verified})")

    def test_load(self):
        """Учитываются свежие и без года; чужая, старая и из удалённого источника — нет."""
        conn = self.conn

        @asynccontextmanager
        async def connection():
            yield conn

        out = asyncio.run(coauthors.load_coauthors("dev", [13, 99], 2021, connection))
        self.assertEqual(sorted(t for t, _, _ in out[13]), ["Без года", "Своя новая"])
        out = asyncio.run(coauthors.load_coauthors("dev", [13], None, connection))
        self.assertEqual(len(out[13]), 3)


if __name__ == "__main__":
    unittest.main()

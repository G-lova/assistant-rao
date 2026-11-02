"""Тесты справочника организаций и псевдонимов (search_experts.org_directory, search_experts.org_aliases).

Примеры — из выгрузок users от 27.10 и 02.11.2026. Запись псевдонимов проверяется на настоящем Postgres
(``PE_TEST_PG=host:port``).
"""
import asyncio
import os
import unittest
from unittest import mock

import pandas as pd

from search_experts import org_aliases
from search_experts.conflict_detector import ConflictDetector
from search_experts.org_directory import OrgDirectory, _core, covers, resolve_frame, same_stem
from tests import pg_schema
from tests.pg_psql import PsqlConn

PG = os.getenv("PE_TEST_PG")

ENTRIES = [
    ('ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ «РОССИЙСКИЙ ЭКОНОМИЧЕСКИЙ '
     'УНИВЕРСИТЕТ ИМЕНИ Г.В. ПЛЕХАНОВА»', "7705043493", "770501001", "customer"),
    ("ФГБОУ ВО РЭУ им. Г.В. Плеханова", "7705043493", "770501001", "alias"),
    ('ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ «НОВОСИБИРСКИЙ ГОСУДАРСТВЕННЫЙ '
     'ТЕХНИЧЕСКИЙ УНИВЕРСИТЕТ»', "5404105174", "540401001", "customer"),
    ('ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ «ИРКУТСКИЙ ГОСУДАРСТВЕННЫЙ '
     'УНИВЕРСИТЕТ»', "3808013278", "380801001", "customer"),
    ('ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ «САНКТ-ПЕТЕРБУРГСКИЙ '
     'ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ»', "7801002274", "780101001", "expert_work"),
    ('ГОСУДАРСТВЕННОЕ АВТОНОМНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ ГОРОДА МОСКВЫ "МОСКОВСКИЙ ГОРОДСКОЙ '
     'ПЕДАГОГИЧЕСКИЙ УНИВЕРСИТЕТ"', "7719072772", "771901001", "expert_diplom"),
    ('ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ "РОССИЙСКАЯ АКАДЕМИЯ НАРОДНОГО '
     'ХОЗЯЙСТВА И ГОСУДАРСТВЕННОЙ СЛУЖБЫ ПРИ ПРЕЗИДЕНТЕ РОССИЙСКОЙ ФЕДЕРАЦИИ"', "7729050901", "772901001", "customer"),
    ("мусор", "123", None, "customer"),
]


class CoreAndCoverTest(unittest.TestCase):
    """Ядро названия и сравнение по словам."""

    def test_core(self):
        """Снимаются ОПФ, регион, «Российской Федерации»; сокращение после имени («ГАУ») остаётся."""
        self.assertEqual(_core(ENTRIES[5][0]), "московский городской педагогический университет")
        self.assertEqual(_core("гаоу во г.москвы московский городской педагогический университет"),
                         "московский городской педагогический университет")
        self.assertEqual(_core("ФГБОУ ВО Санкт-Петербургский ГАУ"), "санкт петербургский гау")
        self.assertEqual(_core("ГБОУ ВПО Первый МГМУ им. И.М. Сеченова Минздрава РФ"), "первый мгму им сеченова")

    def test_same_stem(self):
        """Падежи — одна основа; разные слова — нет."""
        self.assertTrue(same_stem("академии", "академия"))
        self.assertTrue(same_stem("государственного", "государственный"))
        self.assertFalse(same_stem("медицинский", "московский"))

    def test_covers(self):
        """Лишнее слово с любой стороны различает организации; «им …», «национальный исследовательский», филиал — нет."""
        self.assertFalse(covers("иркутский государственный медицинский университет", "иркутский государственный университет"))
        self.assertFalse(covers("новосибирский государственный университет", "новосибирский государственный технический университет"))
        self.assertTrue(covers("астраханский государственный университет", "астраханский государственный университет им татищева"))
        self.assertTrue(covers("саратовский государственный университет им чернышевского",
                               "саратовский национальный исследовательский государственный университет им чернышевского"))
        self.assertTrue(covers("северо кавказский институт филиал российской академии народного хозяйства",
                               "российская академия народного хозяйства"))
        self.assertTrue(covers("томский государственный архитектурно строительный унивесритет",
                               "томский государственный архитектурно строительный университет"))


class ResolveTest(unittest.TestCase):
    """Восстановление ИНН по справочнику."""

    @classmethod
    def setUpClass(cls):
        """Справочник из примеров."""
        cls.d = OrgDirectory(ENTRIES)

    def resolve(self, name):
        """ИНН по названию с настройками по умолчанию (0.8, зазор 0.03, филиалы 0.7)."""
        m = self.d.resolve(name, 0.8, 0.03, branch_threshold=0.7)
        return m.inn if m else None

    def test_entries(self):
        """Некорректный ИНН не попадает в справочник, повторы схлопываются."""
        self.assertEqual(len(self.d), 7)

    def test_resolve(self):
        """Аббревиатура через псевдоним, опечатка, регион в официальном названии, филиал."""
        self.assertEqual(self.resolve("РЭУ им. Г.В. Плеханова"), "7705043493")
        self.assertEqual(self.resolve("российский эконимический университет имени г.в.плеханова"), "7705043493")
        self.assertEqual(self.resolve("Московский городской педагогический университет"), "7719072772")
        self.assertEqual(self.resolve("Северо-Кавказский институт-филиал Российской академии народного хозяйства и "
                                      "государственной службы при Президенте Российской Федерации"), "7729050901")

    def test_no_false_match(self):
        """Другая организация с похожим названием, «ГАУ» и пустое название — без ИНН."""
        self.assertIsNone(self.resolve("Иркутский государственный медицинский университет"))
        self.assertIsNone(self.resolve("Новосибирский государственный университет"))
        self.assertIsNone(self.resolve("ФГБОУ ВО Санкт-Петербургский ГАУ"))
        self.assertIsNone(self.resolve(""))

    def test_ambiguous(self):
        """Одно название у двух организаций (разные ИНН) — неоднозначно, ИНН не восстанавливается."""
        d = OrgDirectory([("Институт развития образования", "7604014626", None, "customer"),
                          ("Институт развития образования", "5751009831", None, "customer")])
        self.assertIsNone(d.resolve("ГАУ ДПО Институт развития образования", 0.8, 0.03))

    def test_resolve_frame_and_conflict(self):
        """Кандидат без ИНН получает ИНН из справочника → конфликт guess_inn_work; с ИНН в анкете — не трогается."""
        df = pd.DataFrame({
            "expert_id": [1, 2], "expert_name": ["а", "б"], "expertise_name": ["в", "в"],
            "expert_organization": ["рэу им. г.в. плеханова", "рэу им. г.в. плеханова"],
            "expert_organization_inn": [None, "7801002274"], "expert_diplom": ["", ""], "expert_diplom_inn": [None, None],
            "expertise_organization": [ENTRIES[0][0].lower()] * 2, "expertise_inn": ["7705043493"] * 2,
            "expertise_kpp": ["770501001"] * 2, "exucutorContract": ["", ""]})
        df = resolve_frame(df, self.d, 0.8, 0.03, 0.7)
        self.assertEqual([v if isinstance(v, str) else None for v in df["expert_organization_inn_guess"]], ["7705043493", None])
        self.assertIn("alias", df.loc[0, "expert_organization_guess"])
        self.assertEqual(ConflictDetector.find_requisite_conflicts(df), {1: ["guess_inn_work"]})

    def test_guess_mismatch_falls_back_to_names(self):
        """ИНН из справочника не совпал — место работы всё равно сравнивается по названиям."""
        row = {"expert_name": "а", "expertise_name": "в", "expert_organization": "фгбоу во тульский государственный университет",
               "expert_organization_inn": None, "expert_organization_inn_guess": "1111111111", "expert_diplom": "",
               "expertise_organization": "тульский государственный университет", "expertise_inn": "7106003011",
               "expertise_kpp": "710601001", "exucutorContract": ""}
        self.assertEqual(ConflictDetector.organization_conflict_reasons(row), ["name_work"])


class LearnTest(unittest.TestCase):
    """Пополнение псевдонимов по изменениям анкет."""

    def test_learn(self):
        """ИНН появился — прежнее написание становится псевдонимом; совпадающее с официальным — нет."""
        prev = {(1, "organization"): {"name": "ФГБОУ ВО ЗабГУ", "inn": None},
                (2, "organization"): {"name": "Забайкальский государственный университет", "inn": None},
                (3, "organization"): {"name": "ООО Ромашка", "inn": "7725305387"}}
        rows = [{"expert_id": 1, "organization": 'ФГБОУ ВО "ЗАБАЙКАЛЬСКИЙ ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ"',
                 "organization_inn": "7534000257", "organization_kpp": "753601001"},
                {"expert_id": 2, "organization": 'ФГБОУ ВО "ЗАБАЙКАЛЬСКИЙ ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ"',
                 "organization_inn": "7534000257", "organization_kpp": "753601001"},
                {"expert_id": 3, "organization": "ООО Лютик", "organization_inn": "7700000001"}]
        aliases, state = org_aliases.learn(prev, rows)
        self.assertEqual(aliases, [("ФГБОУ ВО ЗабГУ", "7534000257", "753601001")])
        self.assertEqual(len(state), 6)   # 3 эксперта × (место работы, диплом)


@unittest.skipUnless(PG, "нужен PE_TEST_PG=host:port")
class AliasesDbTest(unittest.TestCase):
    """Миграция 012 и запись псевдонимов на настоящем Postgres."""

    @classmethod
    def setUpClass(cls):
        """Создаёт тестовую схему (все миграции, включая начальные псевдонимы)."""
        host, port = PG.split(":")
        cls.conn = PsqlConn(host, int(port))
        pg_schema.build(cls.conn)

    def test_seed_and_learn(self):
        """Начальные данные загружены; первый запуск запоминает анкеты, второй — добавляет псевдоним."""
        seeded = int(self.conn._run("SELECT count(*) FROM org_name_aliases WHERE source = 'users_2026-10-27'"))
        self.assertGreater(seeded, 50)
        rows1 = [{"expert_id": 77, "organization": "ФГБОУ ВО ТестГУ", "organization_inn": None}]
        rows2 = [{"expert_id": 77, "organization": "ТЕСТОВЫЙ ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ", "organization_inn": "7712345678"}]

        async def step(rows):
            prev_rows = await self.conn.fetch("SELECT expert_id, field, name, inn FROM expert_org_names WHERE env = 'dev'")
            prev = {(int(r["expert_id"]), r["field"]): dict(r) for r in prev_rows}
            aliases, state = org_aliases.learn(prev, rows)
            return await org_aliases.save(self.conn, "dev", aliases, state)

        self.assertEqual(asyncio.run(step(rows1)), 0)
        self.assertEqual(asyncio.run(step(rows2)), 1)
        self.assertEqual(asyncio.run(step(rows2)), 0)
        self.assertEqual(self.conn._run("SELECT inn FROM org_name_aliases WHERE alias = 'ФГБОУ ВО ТестГУ'"), "7712345678")

        from search_experts.org_directory import load_aliases
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def connection():
            yield self.conn

        aliases = asyncio.run(load_aliases(connection))
        self.assertEqual(len(aliases), seeded + 1)


if __name__ == "__main__":
    unittest.main()


class PipelineGuessTest(unittest.TestCase):
    """ScoringPipeline.attach_org_guesses: справочник применяется, ошибка справочника подбор не ломает."""

    def test_attach(self):
        """Кандидат без ИНН получает ИНН из справочника; при сбое загрузки датафрейм возвращается как был."""
        from tests.stubs import stubbed_import

        with stubbed_import("search_experts.pipeline") as module:
            p = module.ScoringPipeline.__new__(module.ScoringPipeline)
            p.environment, p.data_fetcher = "dev", None
            df = pd.DataFrame({"expert_id": [1], "expert_organization": ["рэу им. г.в. плеханова"],
                               "expert_organization_inn": [None], "expert_diplom": [""], "expert_diplom_inn": [None]})

            async def directory(env, fetcher, connection=None):
                return OrgDirectory(ENTRIES)

            with mock.patch("search_experts.org_directory.get_directory", side_effect=directory):
                out = asyncio.run(p.attach_org_guesses(df.copy()))
            self.assertEqual(out.loc[0, "expert_organization_inn_guess"], "7705043493")

            async def broken(*a, **k):
                raise ConnectionError("портал недоступен")

            with mock.patch("search_experts.org_directory.get_directory", side_effect=broken):
                out = asyncio.run(p.attach_org_guesses(df.copy()))
            self.assertNotIn("expert_organization_inn_guess", out.columns)

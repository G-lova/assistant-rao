"""Тесты сопоставления организаций эксперта и заказчика в поиске конфликта интересов."""
import unittest

import pandas as pd

from search_experts.conflict_detector import ConflictDetector


class SameOrganizationTest(unittest.TestCase):
    """Проверки `ConflictDetector.same_organization` на реальных записях из users."""

    def assertSame(self, a, b):
        """Утверждает, что названия признаны одной организацией."""
        self.assertTrue(ConflictDetector.same_organization(a, b), f"{a!r} vs {b!r}")

    def assertDifferent(self, a, b):
        """Утверждает, что названия признаны разными организациями."""
        self.assertFalse(ConflictDetector.same_organization(a, b), f"{a!r} vs {b!r}")

    def test_abbreviated_vs_full_legal_form(self):
        """Кейс эксперта 254 / экспертиз 7073, 7161, 7162: «ФГБОУ ВО» против полной записи ОПФ."""
        self.assertSame(
            'ФГБОУ ВО "Тульский государственный университет"',
            "ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ ОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ ВЫСШЕГО ОБРАЗОВАНИЯ "
            "«ТУЛЬСКИЙ ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ»",
        )

    def test_honorary_name_typo_abbreviation_branch(self):
        """Хвост «имени …», опечатка, аббревиатура в конце и филиал — та же организация."""
        self.assertSame("Астраханский государственный университет",
                        "Астраханский государственный университет им. В.Н. Татищева")
        self.assertSame("Российский эконимический университет им. Г.В. Плеханова",
                        "Российский экономический университет имени Г.В. Плеханова")
        self.assertSame("Ростовский государственный экономический университет (РИНХ)",
                        "Ростовский государственный экономический университет")
        self.assertSame("Елабужский институт (филиал) ФГАОУ ВО «Казанский (Приволжский) федеральный университет»",
                        "Казанский (Приволжский) федеральный университет")

    def test_different_organizations(self):
        """Разные вузы с общими словами не должны совпадать."""
        self.assertDifferent("Воронежский государственный университет",
                             "Воронежский государственный университет инженерных технологий")
        self.assertDifferent("Омский государственный технический университет",
                             "Брянский государственный технический университет")
        self.assertDifferent("Забайкальский государственный университет", "Байкальский государственный университет")
        self.assertDifferent("Сибирский федеральный университет",
                             "Сибирский государственный университет науки и технологий им. М.Ф. Решетнева")
        self.assertDifferent("Российская академия образования",
                             "Российская академия народного хозяйства и государственной службы при Президенте РФ")
        self.assertDifferent("Институт цитологии РАН", "Институт археологии РАН")

    def test_empty_values(self):
        """Пустые значения и однословные ядра не дают конфликта."""
        self.assertDifferent(None, "Тульский государственный университет")
        self.assertDifferent("ФГБОУ ВО Университет", "Тульский государственный университет")


class FindOrganizationConflictsTest(unittest.TestCase):
    """Проверки `ConflictDetector.find_organization_conflicts`."""

    def test_customer_and_executor(self):
        """Исключается эксперт из организации заказчика и эксперт из организации исполнителя."""
        df = pd.DataFrame({
            "expert_id": [254, 300, 301],
            "expert_organization": ['фгбоу во тульский государственный университет', 'ооо ромашка',
                                    'московский государственный университет'],
            "expertise_organization": ["федеральное государственное бюджетное образовательное учреждение высшего "
                                       "образования тульский государственный университет"] * 3,
            "exucutorContract": ['общество с ограниченной ответственностью "Ромашка Плюс"', None, None],
        })
        self.assertEqual(ConflictDetector.find_organization_conflicts(df), {254})  # «Ромашка» ≠ «Ромашка Плюс»
        df.loc[0, "exucutorContract"] = 'ООО "Ромашка"'
        df.loc[1, "exucutorContract"] = 'ООО "Ромашка"'
        self.assertEqual(ConflictDetector.find_organization_conflicts(df), {254, 300})


class RequisitesTest(unittest.TestCase):
    """Конфликт интересов по ИНН/КПП места работы и диплома, без реквизитов — по названиям."""

    CUSTOMER = {
        "expertise_name": "маликов а.а.",
        "expertise_organization": "федеральное государственное бюджетное образовательное учреждение высшего "
                                  "образования тульский государственный университет",
        "expertise_inn": "7106003011", "expertise_kpp": "710601001", "exucutorContract": "",
    }

    def row(self, **expert):
        """Строка кандидата: заказчик ТулГУ + поля эксперта."""
        base = {"expert_id": 1, "expert_name": "иванов иван", "expert_organization": "", "expert_diplom": "",
                "expert_organization_inn": None, "expert_organization_kpp": None,
                "expert_diplom_inn": None, "expert_diplom_kpp": None}
        return {**self.CUSTOMER, **base, **expert}

    def test_normalize(self):
        """ИНН и КПП из числа, с пробелами, с потерянным ведущим нулём; мусор — None."""
        from search_experts.conflict_detector import normalize_inn, normalize_kpp
        self.assertEqual(normalize_inn(7106003011.0), "7106003011")
        self.assertEqual(normalize_inn(" 710 600 3011 "), "7106003011")
        self.assertEqual(normalize_inn("274062111"), "0274062111")
        self.assertIsNone(normalize_inn("0000000000"))
        self.assertIsNone(normalize_inn("123"))
        self.assertIsNone(normalize_inn(float("nan")))
        self.assertEqual(normalize_kpp("7701AB001"), "7701AB001")
        self.assertEqual(normalize_kpp(27401001), "027401001")
        self.assertIsNone(normalize_kpp(""))

    def test_requisites_match(self):
        """Тот же ИНН и КПП — same; другой КПП — branch; разные ИНН — False; нет ИНН — None."""
        m = ConflictDetector.requisites_match
        self.assertEqual(m("7106003011", "710601001", "7106003011", "710601001"), "same")
        self.assertEqual(m("7106003011", None, "7106003011", "710601001"), "same")
        self.assertEqual(m("5408106370", "540801001", "5408106370", "540843001"), "branch")
        self.assertFalse(m("7106003011", "710601001", "7107000000", "710701001"))
        self.assertIsNone(m(None, None, "7106003011", "710601001"))

    def test_expert_254_by_inn(self):
        """Эксперт 254: ИНН места работы и диплома = ИНН заказчика 739 (экспертизы 7073, 7161, 7162)."""
        r = self.row(expert_organization="фгбоу во тульский государственный университет",
                     expert_organization_inn="7106003011", expert_organization_kpp="710601001",
                     expert_diplom_inn="7106003011", expert_diplom_kpp="710601001")
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["inn_work", "inn_diplom"])

    def test_inn_overrides_names(self):
        """ИНН есть и не совпал — похожее название не даёт конфликта (тёзки-вузы, ошибки в названии)."""
        r = self.row(expert_organization="тульский государственный университет",
                     expert_organization_inn="7107000000", expert_organization_kpp="710701001")
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), [])

    def test_fallback_to_names(self):
        """Реквизитов нет у эксперта или у заказчика — сравнение по названиям, как раньше."""
        r = self.row(expert_organization="фгбоу во тульский государственный университет")
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["name_work"])
        r = self.row(expert_organization="фгбоу во тульский государственный университет",
                     expert_organization_inn="7106003011", expertise_inn=None)
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["name_work"])

    def test_branch_setting(self):
        """Головная организация ↔ филиал: конфликт по умолчанию, при CONFLICT_INN_SAME_KPP_ONLY — нет."""
        from unittest import mock
        from configs import settings
        r = self.row(expert_organization_inn="7106003011", expert_organization_kpp="710645001")
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["inn_work_branch"])
        with mock.patch.object(settings, "CONFLICT_INN_SAME_KPP_ONLY", True):
            self.assertEqual(ConflictDetector.organization_conflict_reasons(r), [])

    def test_org_name_fuzzy_setting(self):
        """Полные названия с общей ОПФ: fuzz.ratio ≥ 85 даёт ложный конфликт; при CONFLICT_ORG_NAME_FUZZY = False — нет."""
        from unittest import mock
        from configs import settings
        r = self.row(expert_organization="федеральное государственное бюджетное образовательное учреждение высшего "
                                         "образования тюменский государственный университет")
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["name_work"])
        with mock.patch.object(settings, "CONFLICT_ORG_NAME_FUZZY", False):
            self.assertEqual(ConflictDetector.organization_conflict_reasons(r), [])
            r = self.row(expert_organization="фгбоу во тульский государственный университет")
            self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["name_work"])

    def test_executor_and_person_always_by_names(self):
        """Исполнитель контракта и ФИО — по названиям и при наличии ИНН."""
        r = self.row(expert_organization="ооо ромашка", expert_organization_inn="7725305387",
                     exucutorContract='ооо "ромашка"')
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["name_executor"])
        r = self.row(expert_name="маликов а.а.", expert_organization_inn="7725305387")
        self.assertEqual(ConflictDetector.organization_conflict_reasons(r), ["name_person"])

    def test_find_requisite_conflicts(self):
        """По датафрейму: только эксперты с конфликтом и их причины."""
        df = pd.DataFrame([self.row(expert_id=254, expert_organization_inn="7106003011"),
                           self.row(expert_id=300, expert_organization_inn="7725305387"),
                           self.row(expert_id=301, expert_organization="тульский государственный университет")])
        self.assertEqual(ConflictDetector.find_requisite_conflicts(df), {254: ["inn_work"], 301: ["name_work"]})


if __name__ == "__main__":
    unittest.main()

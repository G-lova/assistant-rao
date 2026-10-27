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


if __name__ == "__main__":
    unittest.main()

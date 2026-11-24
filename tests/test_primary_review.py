"""Тесты проверок первичного заключения (knowledge_store.primary_review)."""
import unittest

from knowledge_store import primary_review as pr


class PrimaryReviewTests(unittest.TestCase):
    """Объективные дефекты заключений закупки и отчётных документов."""

    def test_clean_opinion_has_no_issues(self):
        """Заполненное заключение без нулей и заглушек замечаний не даёт."""
        data = {"documents": "Извещение, проект контракта, обоснование НМЦК", "field2_1_1": 1, "field2_1_2": 1,
                "field3": "", "field4": "На основании представленной документации целесообразно осуществить закупку."}
        self.assertEqual(pr.review_opinion(data), [])

    def test_zero_without_remarks_and_junk(self):
        """«0» при пустых замечаниях, пустой перечень документов и «тест» находятся."""
        data = {"documents": "", "field2_1_1": 0, "field2_1_2_text": "тест", "field3": "", "field4": "____"}
        codes = {i.code for i in pr.review_opinion(data)}
        self.assertEqual(codes, {"EMPTY_DOCS", "JUNK_TEXT", "ZERO_NO_REMARKS", "CONCLUSION_TEMPLATE"})

    def test_report_form(self):
        """Заключение по отчётным документам: нет позиций / позиция без названия."""
        self.assertIn("NO_ITEMS", {i.code for i in pr.review_opinion({"field1_1": "Акт от 01.02.2026 № 5", "field1_3": []})})
        data = {"field1_1": "Акт от 01.02.2026 № 5", "field1_3": [{"name": ""}]}
        self.assertIn("ITEM_NO_NAME", {i.code for i in pr.review_opinion(data)})

    def test_normalize(self):
        """Булевы и строковые значения приводятся к 1/0/2."""
        self.assertEqual([pr.normalize(v) for v in (True, "false", 2, "x", None)], [1, 0, 2, None, None])


class TestExpertsTests(unittest.TestCase):
    """Тестовые пользователи исключаются из обучения."""

    def test_is_test_expert(self):
        """Идентификаторы из настроек (в том числе строкой, как в CSV) — тестовые; остальные и мусор — нет."""
        from scripts.eval_primary_opinion import is_test_expert
        self.assertTrue(is_test_expert("3") and is_test_expert(1638) and is_test_expert("1538") and is_test_expert(1806))
        self.assertFalse(is_test_expert("7"))      # заглушки в профиле, но реальные заключения: оставлен
        self.assertFalse(is_test_expert("1645") or is_test_expert("NULL") or is_test_expert(None))


class FindTestExpertsTests(unittest.TestCase):
    """Поиск тестовых экспертов по косвенным признакам."""

    def test_signals(self):
        """Имя «Тест», заглушки в реквизитах, «Моя работа» и повторяющиеся буквы находятся; обычный эксперт — нет."""
        from scripts.find_test_experts import find_candidates, is_placeholder
        plain = {"id": "10", "role": "expert", "name": "Иванова Анна", "email": "a@mail.ru", "inn": "123456789012", "phone": "79161234567"}
        fake = {"id": "7", "role": "raoExpert", "name": "Кулешова С.", "phone": "71111111111", "inn": "111111111111"}
        named = {"id": "8", "role": "expert", "name": "Тестовый"}
        junk = {"id": "9", "role": "expert", "name": "Ковалёва gggggggggg"}
        got = find_candidates([plain, fake, named, junk, {"id": "1", "role": "expertise", "name": "Тест"}])
        self.assertEqual(got, {"7": ["profile"], "8": ["name"], "9": ["junk-name"]})
        self.assertTrue(is_placeholder("11111111111") and not is_placeholder("79161234567"))

    def test_method_mismatch(self):
        """Иной метод НМЦК — методологическая ошибка; пустое значение, одинаковый метод и отчётные документы — нет."""
        base = {"field2_1_1": 1}
        self.assertEqual(pr.compare_method({**base, "field2_2_2_0": 3}, {**base, "field2_2_2_0": "1"}).code, "METHOD_MISMATCH")
        self.assertIsNone(pr.compare_method({**base, "field2_2_2_0": "1"}, {**base, "field2_2_2_0": 1}))
        self.assertIsNone(pr.compare_method(base, {**base, "field2_2_2_0": 1}))

    def test_report_facts_and_penalty(self):
        """Анкетные факты и суммы неустойки отчётных документов сравниваются отдельно от критериев."""
        base = {"field1_3": [], "field5_0": False, "field5_0_1": False, "field5_1_11": True, "field5_1_4": 241.04, "field5_1_7": "1 288,97",
                "field5_1_10": 3000, "field6_1_4_1": "", "field5_1_8_0": True}
        # одинаковые данные — замечаний нет; служебное поле (автозаполнение) не сравнивается
        self.assertEqual(pr.compare_report_facts(base, {**base, "field5_1_8_0": False}), [])
        # скрытый флаг «заполнить автоматически» (field5_0_1 / field6_0_1) зависит от версии формы и не сравнивается
        self.assertEqual(pr.compare_report_facts({**base, "field5_0_1": True, "field6_0_1": True}, base), [])
        # неверный ответ о требовании субподряда из СМП → FACT_MISMATCH с названием поля
        got = pr.compare_report_facts({**base, "field5_0": True}, base)
        self.assertEqual([i.code for i in got], ["FACT_MISMATCH"])
        self.assertIn("СМП", got[0].message)
        # сумма пеней отличается больше допуска (1 руб.) → PENALTY_MISMATCH; в пределах допуска — нет
        got = pr.compare_report_facts({**base, "field5_1_4": 300}, base)
        self.assertEqual([i.code for i in got], ["PENALTY_MISMATCH"])
        self.assertEqual(pr.compare_report_facts({**base, "field5_1_4": 241.9}, base), [])
        # сумма заказчика: пусто = 0; работы/НИР хранят суммы под field6_1_*
        self.assertEqual(len(pr.compare_report_facts({**base, "field6_1_4_1": "500,00"}, base)), 1)
        works = {"field1_3": [], "field6_1_11": True, "field6_1_4": 100, "field6_1_7": 0, "field6_1_10": 0}
        self.assertEqual([i.code for i in pr.compare_report_facts({**works, "field6_1_4": 150}, works)], ["PENALTY_MISMATCH"])
        # расчёт не требуется у одного заключения: суммы не сверяются, остаётся только расхождение факта
        got = pr.compare_report_facts({**base, "field5_1_11": False, "field5_1_4": 0}, base)
        self.assertEqual([i.code for i in got], ["FACT_MISMATCH"])
        # закупки и пустые значения — без замечаний
        self.assertEqual(pr.compare_report_facts({"field2_1_1": 1}, {"field2_1_1": 0}), [])
        self.assertEqual(pr.compare_report_facts({"field1_3": [], "field5_0": None}, {"field1_3": [], "field5_0": True}), [])


if __name__ == "__main__":
    unittest.main()

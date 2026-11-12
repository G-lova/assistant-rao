"""Организации для ссылок в заключениях модели (``resume_entities``, 12.11.2026)."""
import unittest

from risk_monitoring import resume_entities as re_


class ResumeEntitiesTest(unittest.TestCase):
    """Сбор и отбор организаций для дашбордов."""

    def test_purchase(self):
        """Закупка: заказчик с id справочника и поставщик; пустой поставщик не пишется."""
        p = {"customer": {"name": "НИУ «МЭИ»", "inn": "7722019652", "kpp": "772201001", "organisation_id": 15},
             "supplier": {"name": None, "inn": None}}
        self.assertEqual(re_.for_purchase(p), [{"role": "customer", "name": "НИУ «МЭИ»", "inn": "7722019652",
                                                "kpp": "772201001", "organisation_id": 15}])

    def test_organisation_with_top(self):
        """Заказчик и его крупнейшие поставщики; повтор ИНН сливается."""
        sec = {"top_suppliers": [{"inn": "2455037921", "name": "МЕДСИБТОРГ"}, {"inn": "2455037921", "name": "x"}]}
        out = re_.for_organisation("customer", {"name": "РНЦХ", "inn": "7704030124", "organisation_id": 1739}, sec)
        self.assertEqual([(e["role"], e.get("inn")) for e in out],
                         [("customer", "7704030124"), ("supplier", "2455037921")])
        self.assertEqual(out[1]["name"], "МЕДСИБТОРГ")

    def test_daily(self):
        """Сводка дня: заказчики пунктов по card_id и поставщики с оценкой."""
        items = [{"card_id": 1, "customer": "МГЛУ", "organisation_id": 1900, "customer_inn": "7704011964"},
                 {"card_id": 2, "customer": "Другой", "organisation_id": 5}]
        s = {"attention": [{"card_id": 1}], "priorities": [{"card_id": 1}], "contracts": {},
             "suppliers": [{"name": "БАРИОН", "inn": "5401234567", "organisation_id": 2168}]}
        out = re_.for_daily(s, items, 10)
        self.assertEqual({(e["role"], e.get("organisation_id")) for e in out}, {("customer", 1900), ("supplier", 2168)})

    def test_mentioned(self):
        """Упоминание по названию без формы и кавычек или по ИНН; общее короткое — нет."""
        e = {"role": "customer", "name": 'ФГБОУ ВО "НИУ "МЭИ""', "inn": "7722019652"}
        self.assertTrue(re_.mentioned(e, "Закупки НИУ «МЭИ» выросли"))
        self.assertTrue(re_.mentioned({"role": "supplier", "inn": "2455037921"}, "ИНН 2455037921"))
        self.assertFalse(re_.mentioned({"role": "customer", "name": "АО ИК"}, "АО ИК и другие"))

    def test_period(self):
        """Сводка периода: только организации ежедневных сводок, упомянутые в тексте."""
        dailies = [{"resume_entities": [{"role": "customer", "name": "МГЛУ", "organisation_id": 1900},
                                        {"role": "customer", "name": "КБГУ", "organisation_id": 1754}]}]
        out = re_.for_period(dailies, "За неделю у МГЛУ три закупки в красной зоне.")
        self.assertEqual([e["organisation_id"] for e in out], [1900])


if __name__ == "__main__":
    unittest.main()

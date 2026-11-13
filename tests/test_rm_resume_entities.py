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
        """Сводка периода: упомянутые в тексте — первыми, с упоминанием; остальные — после."""
        dailies = [{"resume_entities": [{"role": "customer", "name": "КБГУ им. Бербекова", "organisation_id": 1754},
                                        {"role": "customer", "name": "Московский государственный лингвистический университет",
                                         "organisation_id": 1900}]}]
        out = re_.for_period(dailies, "За неделю у Московского государственного лингвистического университета три закупки.")
        self.assertEqual([e["organisation_id"] for e in out], [1900, 1754])
        self.assertEqual(out[0]["mentions"], ["Московского государственного лингвистического университета"])
        self.assertNotIn("mentions", out[1])

    def test_declension_mentions(self):
        """Название в другом падеже — упоминание как в тексте (прод, организация 171)."""
        e = {"role": "customer", "name": "ФИЛИАЛ ФГБОУ ВО \"РГСУ\" В Г. МАЙКОПЕ"}
        self.assertEqual(re_.find_mentions({"role": "customer", "name": "Филиал РГСУ в г. Майкопе"},
                                           "Закупочная деятельность филиала РГСУ в г. Майкопе стабильна."),
                         ["филиала РГСУ в г. Майкопе"])
        self.assertEqual(re_.find_mentions(e, "Закупки «РГСУ» в г. Майкопе"), [])

    def test_short_name_and_inn(self):
        """Сокращённое название и ИНН тоже дают упоминания; аббревиатура не склоняется."""
        e = re_.entity("customer", 'ФГБНУ "НИИГБ ИМ. М.М. КРАСНОВА"', "7704036655", short_name="НИИГБ им. М.М. Краснова")
        self.assertEqual(re_.find_mentions(e, "НИИГБ им. М.М. Краснова (ИНН 7704036655) провёл 3 закупки"),
                         ["НИИГБ им. М.М. Краснова", "7704036655"])

    def test_daily_not_capped(self):
        """Сводка дня: все заказчики блока, а не первые DAILY_TOP; упомянутые — первыми после finalize."""
        s = {"customers": [{"name": f"Заказчик номер {i}", "organisation_id": i} for i in range(80)]}
        pool = re_.for_daily(s, [])
        self.assertEqual(len(pool), 80)
        out = re_.finalize(pool, "Рост у организации «Заказчик номер 77».")
        self.assertEqual(out[0]["organisation_id"], 77)


if __name__ == "__main__":
    unittest.main()

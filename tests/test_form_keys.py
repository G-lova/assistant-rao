"""Тесты привязки правил и ключей к названиям критериев в формах разных способов закупки."""
import unittest

from knowledge_store import eis_notice, form_keys


def field(key: str, label: str) -> dict:
    """Поле формы для тестов."""
    return {"field_key": key, "label": label, "value_kind": "compliance"}


class RuleByLabelTests(unittest.TestCase):
    """``eis_notice.rule_for_label``: правило находится по смыслу названия, а не по номеру критерия."""

    def test_competition_numbers_are_kept(self):
        """В конкурсе названия дают те же правила, что и раньше."""
        self.assertEqual(eis_notice.rule_id("1.18. Наличие информации о начальной (максимальной) цене контракта"), "1.18")
        self.assertEqual(eis_notice.rule_id("1.22. Наличие информации о размере аванса"), "1.22")

    def test_shifted_numbers_in_auction(self):
        """В аукционе нумерация сдвинута (нет критериев оценки): «1.39» по смыслу — окончание срока подачи заявок."""
        self.assertEqual(eis_notice.rule_id("1.39. Наличие информации о дате и времени окончания срока подачи заявок на участие в закупке"), "1.41")
        self.assertEqual(eis_notice.rule_id("1.38. Наличие информации о возможности одностороннего отказа от исполнения контракта"), "1.40")

    def test_bank_support_wording_in_single_supplier(self):
        """В единственном поставщике «банковское сопровождение» названо иначе, чем в конкурсе, — правило то же."""
        self.assertEqual(eis_notice.rule_id("1.24. Наличие информации о банковском сопровождении в соответствии со статьей 35"), "1.38")
        self.assertEqual(eis_notice.rule_id("1.36. Наличие информации о банковском и казначейском сопровождении контракта"), "1.38")

    def test_ambiguous_label_gives_no_rule(self):
        """Название, подходящее двум правилам, правила не получает: лучше оставить эксперту, чем ошибиться."""
        label = "1.12. Наличие информации о начальной цене единицы товара с учетом количества, единицы измерения и места поставки"
        self.assertIsNone(eis_notice.rule_for_label(label))

    def test_unknown_label(self):
        """Неизвестное название и пустая строка — без правила."""
        self.assertIsNone(eis_notice.rule_for_label("Итог раздела"))
        self.assertIsNone(eis_notice.rule_for_label(None))


class FormKeysTests(unittest.TestCase):
    """``form_keys.resolve``: ключи особых критериев определяются по названиям."""

    COMPETITION = [field("field2_2_5", "2.5. Соответствие проекта контракта действующему законодательству Российской Федерации"),
                   field("field2_2_3", "2.3. Соответствие требований к содержанию, составу заявки на участие в закупке"),
                   field("field2_2_1_6", "2.1.6. Соответствие содержания представленной документации единому стилю и отсутствию логических ошибок"),
                   field("field2_2_2_0", "Выберите метод обоснования начальной (максимальной) цены контракта, который применяется"),
                   field("field2_1_22", "1.22. Наличие информации о размере аванса (если предусмотрена выплата аванса)")]
    AUCTION = [field("field2_2_5", "2.1.5. Соответствие сроков предоставления исполнителем/подрядчиком документов на согласование"),
               field("field2_2_19", "2.4. Соответствие проекта контракта действующему законодательству Российской Федерации"),
               field("field2_2_18", "2.3. Соответствие требований к содержанию, составу заявки на участие в закупке"),
               field("field2_2_6", "2.1.6. Соответствие содержания представленной документации единому стилю и отсутствие логических ошибок"),
               field("field2_2_7", "Выберите метод обоснования начальной (максимальной) цены контракта, который применяется Заказчиком"),
               field("field2_2_10", "2.2.3. Соответствие метода обоснования НМЦК требованиям ст. 22 44-ФЗ и предмету закупки"),
               field("field2_1_22", "1.22. Наличие информации о размере аванса (если предусмотрена выплата аванса)")]

    def test_competition_keys(self):
        """Для конкурса определяются ключи конкурса."""
        keys = form_keys.resolve(self.COMPETITION)
        self.assertEqual((keys.contract, keys.composition, keys.style, keys.nmck_choice, keys.advance),
                         ("field2_2_5", "field2_2_3", "field2_2_1_6", "field2_2_2_0", "field2_1_22"))

    def test_auction_keys_differ(self):
        """В аукционе проект контракта — field2_2_19, а field2_2_5 — это сроки предоставления документов."""
        keys = form_keys.resolve(self.AUCTION)
        self.assertEqual(keys.contract, "field2_2_19")
        self.assertEqual(keys.composition, "field2_2_18")
        self.assertEqual(keys.style, "field2_2_6")
        self.assertEqual(keys.nmck_choice, "field2_2_7")
        self.assertEqual(keys.method_fit, "field2_2_10")
        self.assertNotIn("field2_2_5", (keys.contract, keys.composition, keys.style))

    def test_missing_criterion_is_none(self):
        """Критерия нет в форме — ключ ``None`` (в чужое поле ничего не пишется)."""
        keys = form_keys.resolve([field("field2_2_5", "2.1.5. Соответствие сроков предоставления документов")])
        self.assertIsNone(keys.contract)

    def test_unlabeled_field_falls_back_to_competition_key(self):
        """Поле без названия (урезанная форма) — ключ конкурса."""
        keys = form_keys.resolve([{"field_key": "field2_2_5", "value_kind": "compliance"}])
        self.assertEqual(keys.contract, "field2_2_5")

    def test_not_applicable_allowed_follows_form(self):
        """«2» допустима для 2.2.7.2/2.2.7.3 формы, а не для ключей конкурса, которые в аукционе означают другое."""
        keys = form_keys.resolve(self.AUCTION + [
            field("field2_2_14", "2.2.7.2. Соответствие потенциальных поставщиков, предоставивших ценовую информацию"),
            field("field2_2_15", "2.2.7.3. Сопоставимость условиям закупки коммерческих и (или) финансовых условий")])
        allowed = keys.not_applicable_allowed()
        self.assertIn("field2_2_14", allowed)
        self.assertNotIn("field2_2_2_5_2", allowed)


if __name__ == "__main__":
    unittest.main()

"""Тесты поддержки 223-ФЗ и формата значений «как у экспертов»."""
import unittest

from knowledge_store import assessment, forms, form_keys, law_context, value_format


def field(key: str, label: str) -> dict:
    """Поле формы для тестов."""
    return {"field_key": key, "label": label, "value_kind": "compliance"}


class Form223Tests(unittest.TestCase):
    """Выбор формы 223-ФЗ по checkType2."""

    def test_competitive_methods(self):
        """Конкурс, аукцион, котировки, запрос предложений 223-ФЗ — одна форма."""
        for check_type in (1, 2, 4, 5, 7, 8, 9, 10):
            self.assertEqual(forms.get_form_code("223-ФЗ", check_type, 6), "223fz_competitive_obj6")

    def test_single_supplier_and_unsupported(self):
        """Единственный поставщик — своя форма; «иные способы» и отчётные документы формы не имеют."""
        self.assertEqual(forms.get_form_code("223-ФЗ", 11, 6), "223fz_single_supplier_obj6")
        self.assertIsNone(forms.get_form_code("223-ФЗ", 12, 6))
        self.assertIsNone(forms.get_form_code("223-ФЗ", 15, 6))

    def test_44fz_unchanged(self):
        """Для 44-ФЗ запрос предложений по-прежнему не поддерживается, формы прежние."""
        self.assertIsNone(forms.get_form_code("44-ФЗ", 9, 6))
        self.assertEqual(forms.get_form_code("44-ФЗ", 1, 6), "44fz_competition_obj6")

    def test_method_from_title(self):
        """Название формы 223-ФЗ → код способа."""
        self.assertEqual(forms.method_code_from_title("223-ФЗ Новый (конкурс, аукцион, котировки)", "223-ФЗ"), "competitive")
        self.assertEqual(forms.method_code_from_title("223-ФЗ Ед поставщик Новый", "223-ФЗ"), "single_supplier")
        self.assertEqual(forms.method_code_from_title("Конкурс новый 44-ФЗ"), "competition")

    def test_label_normalization(self):
        """Название с комбинируемым знаком («и» + U+0306) приводится к обычной «й»."""
        class Ws:
            def cell(self, row, col):
                class C:
                    value = "\u0438\u0306"        # «и» + комбинируемая краткая
                return C()
        self.assertEqual(forms._cell(Ws(), 1, 1), "\u0439")

    def test_comment_is_text(self):
        """«Комментарий к разделу…» — текст, а не критерий наличия."""
        kind, _ = forms.classify("field2_1_16", "Комментарий к разделу «Экспертная оценка…»", False)
        self.assertEqual(kind, forms.KIND_TEXT)


class DerivedKeysTests(unittest.TestCase):
    """Итоговые ключи и ИНН, которых нет в Excel, но есть в данных экспертов."""

    def test_section_parents_and_inn(self):
        """field2_1 (итог раздела 1) и field2_3_8 (итог при дочерних) достраиваются; inn — служебное поле."""
        raw = [("field2_1_1", "1.1. Наличие", "Раздел 1"), ("field2_3_8_1", "3.8.1. Соответствие", "Раздел 3"),
               ("field2_2_4", "2.1. Соответствие", "Раздел 2")]
        keys = {f.field_key: f for f in forms._finalize("223fz_competitive_obj6", raw)}
        self.assertEqual(keys["field2_1"].value_kind, forms.KIND_SECTION)
        self.assertTrue(keys["field2_1"].derived)
        self.assertIn("field2_3_8", keys)
        self.assertEqual(keys["inn"].value_kind, forms.KIND_META)

    def test_value_formats_for_223(self):
        """Формат значений 223-ФЗ взят из выгрузки: часть критериев хранится как true/false."""
        self.assertTrue(value_format.bool_keys("223fz_competitive_obj6"))
        self.assertTrue(value_format.bool_keys("223fz_single_supplier_obj6"))


class Keys223Tests(unittest.TestCase):
    """Ключи раздела 3 и проекта договора в форме 223-ФЗ определяются по названию."""

    FIELDS = [field("field2_3_5", "3.5. Соответствие документации о проведении закупки требованиям Федерального закона от 27.07.2006 №152-ФЗ «О персональных данных»"),
              field("field2_3_7", "3.7. Соответствие документации о проведении закупки требованиям антимонопольного законодательства Российской Федерации"),
              field("field2_2_19_1", "2.7. Соответствие проекта договора действующему законодательству Российской Федерации"),
              field("field2_2_6", "Итог")]

    def test_223_keys(self):
        """3.5 — персональные данные, 3.7 — антимонопольное, 2.7 — проект договора; вида расходов и 44-ФЗ нет."""
        keys = form_keys.resolve(self.FIELDS)
        self.assertEqual((keys.personal, keys.antitrust, keys.contract), ("field2_3_5", "field2_3_7", "field2_2_19_1"))
        self.assertIsNone(keys.expense)
        self.assertIsNone(keys.documentation)


class Law223AssessmentTests(unittest.IsolatedAsyncioTestCase):
    """Правила 44-ФЗ в 223-ФЗ не применяются; промпты называют нужный закон."""

    def tearDown(self):
        """Возвращает закон по умолчанию."""
        law_context.current_law.set(law_context.LAW_44)

    def test_localize(self):
        """В 223-ФЗ «44-ФЗ» в промпте заменяется на «223-ФЗ»."""
        self.assertEqual(law_context.localize("эксперт по закупкам (44-ФЗ)"), "эксперт по закупкам (44-ФЗ)")
        law_context.set_from_form("223fz_competitive_obj6")
        self.assertEqual(law_context.localize("эксперт по закупкам (44-ФЗ)"), "эксперт по закупкам (223-ФЗ)")
        self.assertIn("223-ФЗ", law_context.law_title())

    async def test_contract_rules_off_for_223(self):
        """Нарушения проекта контракта по нормам 44-ФЗ в 223-ФЗ не ищутся."""
        body = ("Приёмка товара оформляется актом по форме КС-2 и КС-3. Оплата по мере поступления денежных средств. "
                "Обеспечение исполнения — банковская гарантия. ") * 120
        fields = [field("field2_2_19_1", "2.7. Соответствие проекта договора действующему законодательству Российской Федерации")]
        docs = {1: {"filename": "Проект договора.docx", "doc_code": None, "text": body}}
        law_context.set_from_form("223fz_competitive_obj6")
        self.assertNotIn("field2_2_19_1", await assessment.compute_assessed(fields, docs, None))
        law_context.set_from_form("44fz_auction_obj6")
        fields44 = [field("field2_2_19", "2.4. Соответствие проекта контракта действующему законодательству Российской Федерации")]
        self.assertIn("field2_2_19", await assessment.compute_assessed(fields44, docs, None))

    def test_presumption_checklist_223(self):
        """Чек-лист состава заявки в 223-ФЗ не ссылается на ст. 42–49 Закона № 44-ФЗ."""
        fk = form_keys.resolve([field("field2_2_18", "2.4. Соответствие требований к содержанию, составу заявки на участие в закупке")])
        law_context.set_from_form("223fz_competitive_obj6")
        spec = assessment.presumption_specs(fk)["field2_2_18"]
        self.assertIn("223-ФЗ", spec[0])
        self.assertNotIn("44-ФЗ", spec[0])


class ValueFormatTests(unittest.TestCase):
    """Булевы значения в формах, где так хранят эксперты."""

    def test_auction_bool(self):
        """В аукционе критерии разделов 2–3 — true/false, раздел 1 — числа."""
        data = {"field2_2_19": 0, "field2_3_7": 1, "field2_1_22": 2, "field2_2_9": 2, "field2_3_1": None}
        out = value_format.to_form_format("44fz_auction_obj6", data)
        self.assertIs(out["field2_2_19"], False)
        self.assertIs(out["field2_3_7"], True)
        self.assertEqual(out["field2_1_22"], 2)          # раздел 1 — числа
        self.assertEqual(out["field2_2_9"], 2)           # смешанное поле: у экспертов и «2»
        self.assertIsNone(out["field2_3_1"])
        self.assertEqual(data["field2_2_19"], 0)         # исходный словарь не изменён

    def test_two_not_expressible_in_bool(self):
        """«2» в булевом поле заменяется пустым значением с пояснением в trace."""
        trace = {"field2_3_3": {"status": "proposed"}}
        out = value_format.to_form_format("44fz_auction_obj6", {"field2_3_3": 2}, trace)
        self.assertIsNone(out["field2_3_3"])
        self.assertIn("не выражается", trace["field2_3_3"]["note"])

    def test_competition_and_223_stay_numeric(self):
        """Для конкурса и 223-ФЗ (выгрузки нет) значения остаются числами."""
        for code in ("44fz_competition_obj6", "223fz_competitive_obj6", None):
            self.assertEqual(value_format.to_form_format(code, {"field2_3_3": 0}), {"field2_3_3": 0})


if __name__ == "__main__":
    unittest.main()

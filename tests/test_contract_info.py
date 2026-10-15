"""Реквизиты контракта по тексту и сохранённому извлечению (``knowledge_store/contract_info.py``)."""
import unittest

from knowledge_store import contract_info as ci

SIGNED = """КОНТРАКТ № 0373200001226000123
на поставку медицинских изделий
г. Москва                                                        «12» марта 2026 г.
Государственное бюджетное учреждение ... именуемое в дальнейшем «Заказчик» ...
2. Цена контракта и порядок расчётов
2.1. Цена Контракта составляет 1 234 567,89 (один миллион ...) рублей, НДС не облагается.
"""

DRAFT = """ПРОЕКТ КОНТРАКТА
Контракт № ______
г. Тверь                                  «___» ________ 20__ г.
Начальная (максимальная) цена контракта составляет 2 000 000,00 рублей.
Цена контракта составляет ____________ рублей.
"""


class ContractInfoTest(unittest.TestCase):
    """Разбор текста, извлечения и выбор документа."""

    def test_signed_text(self):
        """Номер, дата в шапке и цена «Цена Контракта составляет …»."""
        self.assertEqual(ci.from_text(SIGNED), {"contract_number": "0373200001226000123", "amount": "1234567.89",
                                                "date": "2026-03-12"})

    def test_number_with_date(self):
        """«Договор № 15/26-К от 01.04.2026» — дата сразу за номером."""
        r = ci.from_text("Договор поставки № 15/26-К от 01.04.2026 г.\nСтоимость договора: 500000 руб.")
        self.assertEqual((r["contract_number"], r["date"], r["amount"]), ("15/26-К", "2026-04-01", "500000"))

    def test_draft_blanks_and_nmck(self):
        """Незаполненный проект: ни номера, ни даты; НМЦК ценой не считается."""
        self.assertEqual(ci.from_text(DRAFT), {"contract_number": None, "amount": None, "date": None})

    def test_months(self):
        """Май и март различаются."""
        self.assertEqual(ci.parse_date("«5» мая 2026"), "2026-05-05")
        self.assertEqual(ci.parse_date("5 марта 2026"), "2026-03-05")
        self.assertIsNone(ci.parse_date("5 мадрида 2026"))

    def test_extraction(self):
        """Из raw_data: номер-объект, цена по контексту (не НМЦК), дата контракта."""
        ext = {"raw_data": {"contract_number": {"value": "№ 77-2026"},
                            "finances": [{"value": 900000, "context": "НМЦК"},
                                         {"value": 850000.5, "context": "Цена контракта"}],
                            "dates": [{"value": "2026-02-01", "context": "Дата извещения"},
                                      {"value": "2026-02-20", "context": "Дата заключения контракта"}]}}
        self.assertEqual(ci.from_extraction(ext), {"contract_number": "77-2026", "amount": "850000.50",
                                                   "date": "2026-02-20"})

    def test_collect_priority(self):
        """Заключённый контракт раньше проекта; поля, которых нет в контракте, — из проекта; допсоглашение не берётся."""
        docs = [
            {"detected_type": "docProjContractFiles", "filename": "проект.docx",
             "text": "Цена контракта составляет 700 000,00 рублей."},
            {"detected_type": None, "doc_code": "docFiles", "filename": "Доп соглашение к договору.pdf",
             "text": "Договор № 999 от 01.01.2026 г."},
            {"detected_type": "docContractPostTovarFiles", "filename": "контракт.pdf",
             "text": "КОНТРАКТ № 12-П от «3» июня 2026 г.\nусловия..."},
        ]
        r = ci.collect(docs)
        self.assertEqual((r["contract_number"], r["date"], r["amount"]), ("12-П", "2026-06-03", "700000"))
        self.assertEqual(r["sources"]["amount"], "проект.docx: text")

    def test_nothing(self):
        """Документов контракта нет — «0»."""
        r = ci.collect([{"detected_type": "docIzvejenieFiles", "filename": "извещение.pdf", "text": "Контракт № 5"}])
        self.assertEqual({k: r[k] for k in ci.EMPTY}, ci.EMPTY)


if __name__ == "__main__":
    unittest.main()


class EnvLeftoversTest(unittest.TestCase):
    """Предупреждение о переменных .env, перенесённых в код."""

    def test_leftovers(self):
        """Совпадающее значение молчит, отличающееся — в списке."""
        import os
        from unittest import mock

        from configs import settings

        with mock.patch.dict(os.environ, {"PE_FACTS_ENABLED": "true", "MODEL_TEMPERATURE": "0.1",
                                          "PE_LEGACY_ENV": "stage", "RISK_MONITORING_DEBUG": "false"}):
            names = {n for n, _, _ in settings.env_leftovers()}
        self.assertIn("MODEL_TEMPERATURE", names)
        self.assertIn("RISK_MONITORING_DEBUG", names)
        self.assertNotIn("PE_FACTS_ENABLED", names)
        self.assertNotIn("PE_LEGACY_ENV", names)

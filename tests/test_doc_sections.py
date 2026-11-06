"""DOC-001 «нет обязательного раздела» по оглавлению всего документа (``risk_monitoring.doc_sections``)."""
import unittest

from risk_monitoring import doc_sections as ds

FILLER = "Прочие условия изложены в настоящем пункте и не меняют существа обязательств сторон. " * 60


def contract(*sections: str) -> str:
    """Текст контракта с заданными разделами (заголовок и короткий текст)."""
    body = ["КОНТРАКТ № 1", "1. ПРЕДМЕТ КОНТРАКТА", "1.1. Поставщик обязуется поставить товар."]
    for i, (head, line) in enumerate((s.split("|") for s in sections), start=2):
        body += [f"{i}. {head}", f"{i}.1. {line}"]
    return "\n".join(body + ["99. ПРОЧИЕ УСЛОВИЯ", FILLER])


FULL = ("СРОКИ ПОСТАВКИ|Поставка осуществляется в течение 10 дней.",
        "ЦЕНА КОНТРАКТА И ПОРЯДОК РАСЧЁТОВ|Расчёты по безналичной форме.",
        "ПРИЁМКА ТОВАРА|Заказчик проверяет товар.",
        "ОТВЕТСТВЕННОСТЬ СТОРОН|Стороны несут ответственность.")


class DocSectionsTest(unittest.TestCase):
    """Проверка разделов полного текста."""

    def test_full_contract(self):
        """Все обязательные разделы есть — риска нет; заголовки распознаны."""
        text = contract(*FULL)
        self.assertIn("ПРИЁМКА ТОВАРА", ds.headings(text))
        self.assertIsNone(ds.doc001_risk(text, "docContractPostTovarFiles"))

    def test_missing_section(self):
        """Нет раздела и условий об ответственности — риск; цитата — точная строка текста; описание сервиса."""
        text = contract(*FULL[:3])
        risk = ds.doc001_risk(text, "docProjContractFiles")
        self.assertIsNotNone(risk)
        self.assertIn("ответственность сторон", risk["explanation"])
        self.assertIn(risk["fragment"], text)
        self.assertTrue(ds.is_service_risk(risk))
        self.assertFalse(ds.is_service_risk({"description": "В документе отсутствует раздел об оплате"}))

    def test_topic_in_text_without_heading(self):
        """Условия оплаты в тексте без отдельного заголовка — тема найдена (риск только если темы нет нигде)."""
        text = contract(*FULL[:1], *FULL[2:]) + "\nОплата производится в течение 7 рабочих дней."
        self.assertIsNone(ds.doc001_risk(text, "docContractPostTovarFiles"))

    def test_not_applicable(self):
        """Доп. соглашение, приложение (нет «предмета контракта»), короткий текст — не проверяются."""
        text = contract(*FULL[:1])
        self.assertIsNone(ds.doc001_risk(text, "docDopConsentContractFiles"))
        self.assertIsNone(ds.doc001_risk(text.replace("ПРЕДМЕТ КОНТРАКТА", "СПЕЦИФИКАЦИЯ"), "docProjContractFiles"))
        self.assertIsNone(ds.doc001_risk("1. ПРЕДМЕТ КОНТРАКТА\nкоротко", "docProjContractFiles"))


if __name__ == "__main__":
    unittest.main()


class Doc001ScoringTest(unittest.TestCase):
    """DOC-001 модели не идёт в балл закупки и убирается из результата файла; DOC-001 сервиса — идёт."""

    def test_file_and_contract(self):
        """_with_service_doc001: модельный DOC-001 заменяется сервисным; file_risks: модельный — doc001_by_model."""
        from tests.stubs import stubbed_import

        with stubbed_import("risk_monitoring.ai_file_analyzer") as fa:
            text = contract(*FULL[:3])
            model = {"code": "DOC-001", "explanation": "В документе отсутствует раздел о сроках", "fragment": "x"}
            out = fa._with_service_doc001({"risks": [model, {"code": "DOC-007"}]}, text, "docProjContractFiles", True)
            self.assertEqual([r["code"] for r in out["risks"]], ["DOC-007", "DOC-001"])
            self.assertTrue(ds.is_service_risk(out["risks"][1]))
            out = fa._with_service_doc001({"risks": [model]}, text, "docProjContractFiles", False)
            self.assertEqual(out["risks"], [])
        with stubbed_import("risk_monitoring.contract_package") as cp:
            ev = [{"fragment": "1. ПРЕДМЕТ КОНТРАКТА"}]
            files = [{"id": 1, "file_name": "Проект контракта.docx", "xml_source_type": "epNotification",
                      "owner_type": "notice", "risks": [
                          {"rule_id": "DOC-001", "level": 0.6, "confidence": 0.9, "evidence": ev,
                           "description": "В документе отсутствует раздел о сроках"}]},
                     {"id": 2, "file_name": "Контракт.docx", "xml_source_type": "epNotification", "owner_type": "notice",
                      "risks": [{"rule_id": "DOC-001", "level": 0.6, "confidence": 0.9, "evidence": ev,
                                 "description": ds.TOC_PREFIX + ": не найдено ни раздела, ни условий — приёмка."}]}]
            dropped = []
            risks, _ = cp.file_risks(files, {}, dropped)
            self.assertEqual([r["evidence"][0]["src"] for r in risks], ["file:2"])
            self.assertIn({"rule_id": "DOC-001", "file_id": 1, "reason": "doc001_by_model"}, dropped)

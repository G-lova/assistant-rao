"""Тесты замечаний модели к текстовой части заключения (knowledge_store.primary_llm_review, scripts.eval_primary_llm)."""
import asyncio
import unittest

from knowledge_store import primary_llm_review as pl
from scripts import eval_primary_llm as ev


class FakeLlm:
    """Модель-заглушка: возвращает заданный ответ."""

    def __init__(self, findings):
        self.findings = findings

    async def ask(self, system, user, schema=None, **kwargs):
        """Ответ с заданными замечаниями."""
        return {"findings": self.findings}


class LlmReviewTests(unittest.TestCase):
    """Проверка цитат, разбор текста, оценка точности по темам комментария."""

    DATA = {"field3": "1. Нарушение есть, неудачная формулировка пункта.", "field4": "Заключение соответствует требованиям.",
            "field2_1_1_text": "ссылка на п. 5"}

    def test_opinion_text_collects_fields(self):
        """В текст попадают замечания, вывод и текстовые поля критериев."""
        text = pl.opinion_text(self.DATA)
        self.assertIn("[field3]", text)
        self.assertIn("[field4]", text)
        self.assertIn("[field2_1_1_text]", text)
        self.assertEqual(pl.opinion_text({"field1_1": "x"}), "")

    def test_verify_drops_findings_without_real_quote(self):
        """Цитата, которой нет в тексте, и неизвестная категория отбрасываются; цитата ищется без учёта регистра и кавычек."""
        source = pl.opinion_text(self.DATA)
        found = pl.verify([
            {"category": "CONTRADICTION", "quote": "Нарушение ЕСТЬ, неудачная формулировка", "explanation": "а"},
            {"category": "FORMAL", "quote": "такой фразы нет в тексте заключения", "explanation": "б"},
            {"category": "OTHER", "quote": "Нарушение есть, неудачная формулировка", "explanation": "в"},
            {"category": "FORMAL", "quote": "короткая", "explanation": "г"}], source)
        self.assertEqual([f["category"] for f in found], ["CONTRADICTION"])

    def test_verify_limits_per_category(self):
        """Однотипные замечания одной категории ограничиваются MAX_PER_CATEGORY."""
        source = "первая цитата для проверки; вторая цитата для проверки; третья цитата для проверки"
        found = pl.verify([{"category": "FORMAL", "quote": q, "explanation": ""}
                           for q in ("первая цитата для проверки", "вторая цитата для проверки", "третья цитата для проверки")], source)
        self.assertEqual(len(found), pl.MAX_PER_CATEGORY)

    def test_verify_accepts_alias_keys_and_rejects_leaks(self):
        """Ключи type/description принимаются; не объекты и пояснения с рассуждениями («Давайте пересмотрим») отбрасываются."""
        source = "в то врем как предметом закупки является поставка оборудования"
        found = pl.verify([{"type": "FORMAL", "quote": "в то врем как предметом закупки", "description": "опечатка"},
                           "строка", {"type": "FORMAL", "quote": "поставка оборудования", "description": "Давайте пересмотрим цитату"}], source)
        self.assertEqual([(f["category"], f["explanation"]) for f in found], [("FORMAL", "опечатка")])

    def test_report_text_uses_section_fields(self):
        """Отчётные документы: длинные текстовые поля разделов попадают в текст, короткие и служебные — нет."""
        data = {"field1_1": "1. Договор от 01.02.2025 № 5, акт от 02.03.2025 № 7 и иные документы", "field5_3_3": "Комментарий по разделу " * 3,
                "field5_0": "ok", "documents": "x" * 100}
        text = pl.opinion_text(data, report=True)
        self.assertIn("[field1_1]", text)
        self.assertIn("[field5_3_3]", text)
        self.assertNotIn("[field5_0]", text)
        self.assertEqual(pl.opinion_text(data), "")

    def test_review_without_text_does_not_call_model(self):
        """Нет текста — пустой результат без вызова модели; с текстом — только подтверждённые замечания."""
        self.assertEqual(asyncio.run(pl.review(FakeLlm([{"category": "FORMAL", "quote": "x" * 20, "explanation": ""}]), {})), [])
        got = asyncio.run(pl.review(FakeLlm([{"category": "ARGUMENTATION", "quote": "ссылка на п. 5", "explanation": "е"}]), self.DATA))
        self.assertEqual([f["category"] for f in got], ["ARGUMENTATION"])

    def test_trace_shows_where_findings_are_lost(self):
        """Диагностика различает ответ модели и замечания, прошедшие проверку цитат."""
        trace = {}
        asyncio.run(pl.review(FakeLlm([{"category": "FORMAL", "quote": "такой цитаты в тексте нет совсем", "explanation": ""}]),
                              self.DATA, trace=trace))
        self.assertEqual((len(trace["raw"]), len(trace["verified"]), trace["has_text"]), (1, 0, True))
        self.assertEqual(ev.diagnostics([{"trace": trace}]),
                         {"with_text": 1, "answers_with_findings": 1, "raw_findings": 1, "verified_findings": 0})

    def test_llm_findings_flow_into_issues_and_comment(self):
        """Замечания модели выбранных категорий становятся дефектами и попадают в «Формальные недостатки» комментария."""
        from conclusion.primary_review_pipeline import review_expertise
        import json
        findings = [{"category": "FORMAL", "quote": "в то врем как предметом закупки", "explanation": "опечатка в слове «время»"},
                    {"category": "ARGUMENTATION", "quote": "оценочное суждение", "explanation": "нет ссылок"}]
        issues = pl.to_issues(findings, ("FORMAL",))
        self.assertEqual([i.code for i in issues], ["LLM_FORMAL"])
        summary = {"field2_1_1": 1, "field3": "1.1. Замечание", "field4": "Вывод"}
        rows = [{"id": i, "expert_id": i, "data": json.dumps(summary), "updated_at": "2026-10-01", "expert_role": role,
                 "current_range": None, "accept": None, "object": 6, "type_orig": 4, "checkType2": 1}
                for i, role in ((1, "expert"), (9, "raoExpert"))]
        review = review_expertise(rows, extra_issues={1: issues})["reviews"][0]
        self.assertIn("Формальные недостатки", review["comment"])
        self.assertIn("опечатка", review["comment"])
        self.assertIn("FORMAL", [c["code"] for c in review["categories"]])

    def test_score_precision_recall(self):
        """Точность — доля найденных категорий с темой в комментарии; полнота — доля тем, которые нашла модель."""
        rows = [{"found": {"FORMAL"}, "topics": {"FORMAL"}}, {"found": {"FORMAL"}, "topics": set()},
                {"found": set(), "topics": {"FORMAL"}}, {"found": set(), "topics": set()}]
        result = ev.score(rows)["FORMAL"]
        self.assertEqual((result["flagged"], result["precision"], result["recall"], result["base"]), (2, 0.5, 0.5, 0.5))
        self.assertEqual(ev.topics_of("Выявлены противоречия, ошибки оформления"), {"CONTRADICTION", "FORMAL"})


if __name__ == "__main__":
    unittest.main()

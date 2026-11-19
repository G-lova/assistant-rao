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

    def test_review_without_text_does_not_call_model(self):
        """Нет текста — пустой результат без вызова модели; с текстом — только подтверждённые замечания."""
        self.assertEqual(asyncio.run(pl.review(FakeLlm([{"category": "FORMAL", "quote": "x" * 20, "explanation": ""}]), {})), [])
        got = asyncio.run(pl.review(FakeLlm([{"category": "ARGUMENTATION", "quote": "ссылка на п. 5", "explanation": "е"}]), self.DATA))
        self.assertEqual([f["category"] for f in got], ["ARGUMENTATION"])

    def test_score_precision_recall(self):
        """Точность — доля найденных категорий с темой в комментарии; полнота — доля тем, которые нашла модель."""
        rows = [{"found": {"FORMAL"}, "topics": {"FORMAL"}}, {"found": {"FORMAL"}, "topics": set()},
                {"found": set(), "topics": {"FORMAL"}}, {"found": set(), "topics": set()}]
        result = ev.score(rows)["FORMAL"]
        self.assertEqual((result["flagged"], result["precision"], result["recall"], result["base"]), (2, 0.5, 0.5, 0.5))
        self.assertEqual(ev.topics_of("Выявлены противоречия, ошибки оформления"), {"CONTRADICTION", "FORMAL"})


if __name__ == "__main__":
    unittest.main()

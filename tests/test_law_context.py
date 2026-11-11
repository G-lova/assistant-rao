"""Тексты норм и положения заказчика в запросе риск-анализа (law_context, DocumentRiskAnalyzer.context_blocks)."""
import asyncio
import unittest
from datetime import date

from risk_monitoring import law_context as lc
from risk_monitoring.document_risk_analyzer import context_blocks


class RankTest(unittest.TestCase):
    """Отбор норм по сумме сходств с чанками документа."""

    def test_rank(self):
        """Норма, близкая нескольким чанкам, выше нормы с одним лучшим совпадением; ниже порога — отбрасывается."""
        rows = [{"heading": "ч. 6 ст. 96 44-ФЗ", "text": "a", "similarity": 0.7, "document_id": 1},
                {"heading": "ч. 6 ст. 96 44-ФЗ", "text": "a", "similarity": 0.68, "document_id": 1},
                {"heading": "ч. 13 ст. 34 44-ФЗ", "text": "b", "similarity": 0.8, "document_id": 1},
                {"heading": "ст. 1 44-ФЗ", "text": "c", "similarity": 0.4, "document_id": 1}]
        out = lc.rank_norms(rows)
        self.assertEqual([n["address"] for n in out], ["ч. 6 ст. 96 44-ФЗ", "ч. 13 ст. 34 44-ФЗ"])

    def test_format(self):
        """Адрес из начала чанка выносится в скобки, длинный текст обрезается, дата редакции акта."""
        n = {"address": "ч. 6 ст. 96 44-ФЗ", "act_key": "44-ФЗ",
             "text": "ч. 6 ст. 96 44-ФЗ (Обеспечение исполнения контракта): 6. Размер обеспечения " + "x " * 600}
        text = lc.format_norms([n], {"44-ФЗ": "2026-08-04"})
        self.assertTrue(text.startswith("[ч. 6 ст. 96 44-ФЗ] (ред. от 2026-08-04): 6. Размер обеспечения"))
        self.assertTrue(text.endswith("…"))

    def test_acts_and_groups(self):
        """Акты по закону, неизвестный закон — оба; группа типа документа; дата закупки."""
        self.assertIn("ПП-1042", lc.acts_for(44))
        self.assertIn("ПП-1352", lc.acts_for("223"))
        both = lc.acts_for(None)
        self.assertTrue({"44-ФЗ", "223-ФЗ"} <= set(both))
        self.assertEqual(lc.doc_group("docProjContractFiles"), "contract")
        self.assertEqual(lc.doc_group("docOpusObjectZacupFiles"), "notice")
        self.assertEqual(lc.as_date("2026-03-05 00:00:00"), date(2026, 3, 5))
        self.assertIsNone(lc.as_date("null"))


class FakePool:
    """Пул asyncpg: ответы по началу SQL."""

    def __init__(self, editions, nearest, pinned=()):
        """Строки для запросов редакций, ближайших норм и закреплённых норм."""
        self.editions, self.nearest, self.pinned = editions, nearest, list(pinned)
        self.calls = []

    async def fetch(self, sql, *args):
        """Ответ по виду запроса."""
        self.calls.append((sql.strip().split()[0:3], args))
        if "DISTINCT ON (act_key)" in sql:
            return self.editions
        if "CROSS JOIN LATERAL" in sql:
            return self.nearest
        return self.pinned


class ForDocumentTest(unittest.TestCase):
    """LawContext.for_document: редакция на дату закупки, закреплённые нормы, версия."""

    def test_for_document(self):
        """Найденные нормы + недостающая закреплённая (п. 3 ст. 164 НК для контракта), дата редакции в блоке."""
        pool = FakePool(
            editions=[{"id": 1, "act_key": "44-ФЗ", "document_id": 10, "edition_date": date(2026, 8, 4)},
                      {"id": 2, "act_key": "НК", "document_id": 20, "edition_date": date(2026, 8, 4)}],
            nearest=[{"document_id": 10, "heading": "ч. 6 ст. 96 44-ФЗ", "text": "ч. 6 ст. 96 44-ФЗ: 6. Размер",
                      "similarity": 0.8}],
            pinned=[{"act_key": "НК", "edition_date": date(2026, 8, 4), "address": "п. 3 ст. 164 НК",
                     "heading": "Налоговые ставки", "text": "3. … 22 процента"}])
        ctx = lc.LawContext(pool, "emb")
        out = asyncio.run(ctx.for_document(5, 44, date(2026, 3, 1), "docProjContractFiles"))
        self.assertEqual(out["refs"][0], "ч. 6 ст. 96 44-ФЗ")
        self.assertIn("п. 3 ст. 164 НК", out["refs"])
        self.assertIn("22 процента", out["text"])
        self.assertEqual(out["on"], "2026-03-01")
        # редакции на дату закупки запрошены с этой датой; повторный вызов — из кэша
        self.assertEqual(pool.calls[0][1][1], date(2026, 3, 1))
        asyncio.run(ctx.for_document(6, 44, date(2026, 3, 1), None))
        self.assertEqual(sum(1 for c in pool.calls if c[1] and c[1][-1] == date(2026, 3, 1)), 1)
        self.assertEqual(len(out["version"]), 10)

    def test_no_acts(self):
        """Актов не загружено — None (анализ без норм)."""
        self.assertIsNone(asyncio.run(lc.LawContext(FakePool([], []), "emb").for_document(5, 44, None)))


class ClauseFormatTest(unittest.TestCase):
    """Блок положения заказчика."""

    def test_format_clause(self):
        """Присоединение, способы со сроками подачи заявок, способ закупки, пункты."""
        clause = {"registration_number": "1150076900", "version": 33, "use_for_base_date": "2026-11-02",
                  "clause_type": "customer", "head_registration_number": "1130012228",
                  "head": {"name": "АО «Мосводоканал»"},
                  "purchase_methods": [{"name": "Запрос котировок", "application_days": "7", "work_days": True,
                                        "electronic": True}]}
        text = lc.format_clause(clause, [{"point": "12.3", "text": "Обеспечение исполнения до 30 %"}],
                                "Запрос котировок в электронной форме")
        self.assertIn("присоединение к положению АО «Мосводоканал»", text)
        self.assertIn("Запрос котировок (заявки не менее 7 раб. дн.), электронная", text)
        self.assertIn("Способ этой закупки: Запрос котировок в электронной форме.", text)
        self.assertIn("[п. 12.3 положения]: Обеспечение исполнения до 30 %", text)


class BlocksTest(unittest.TestCase):
    """Разделы запроса риск-анализа."""

    def test_blocks(self):
        """Тексты норм и положение — отдельными разделами; без них — пусто."""
        out = context_blocks({"norms": "N", "law_texts": "[ч. 6 ст. 96 44-ФЗ]: …", "clause": "Положение …"})
        self.assertIn("Тексты норм в редакции, действовавшей на дату закупки", out)
        self.assertIn("Положение о закупке заказчика", out)
        self.assertEqual(context_blocks({"law": "44-ФЗ"}), "")


if __name__ == "__main__":
    unittest.main()

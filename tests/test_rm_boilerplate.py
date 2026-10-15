"""Тесты фильтра типовых формулировок (``boilerplate``) и выжимки норм (``norms``) риск-мониторинга."""
import asyncio
import types
import unittest

from risk_monitoring import boilerplate, norms

FRAG = ("Предоставляемые в составе заявки документы должны быть четко напечатаны. Подчистки, дописки, "
        "исправления не допускаются")


class ContainsTest(unittest.TestCase):
    """Почти дословное вхождение цитаты в чанк."""

    def test_contains(self):
        """Та же фраза в другом окружении и с другой пунктуацией — содержится; другая — нет; короткая — не решаем."""
        chunk = "п. 5. " + FRAG.upper().replace(".", ";") + " и заверяются подписью участника. Прочий текст."
        self.assertTrue(boilerplate.contains(FRAG, chunk))
        self.assertFalse(boilerplate.contains(FRAG, "Участник закупки должен иметь опыт строительства мостов"))
        self.assertFalse(boilerplate.contains("или эквивалент", "или эквивалент"))


class CountSourcesTest(unittest.TestCase):
    """Подсчёт разных заказчиков."""

    def test_count(self):
        """Своя закупка и свой заказчик не считаются; экспертиза — отдельный источник; карточка без заказчика — карточка."""
        sources = [{"document_id": 1, "kind": "rm", "source_id": 10}, {"document_id": 2, "kind": "rm", "source_id": 11},
                   {"document_id": 3, "kind": "rm", "source_id": 12}, {"document_id": 4, "kind": "pe", "source_id": 500},
                   {"document_id": 5, "kind": "rm", "source_id": 13}, {"document_id": 6, "kind": "rm", "source_id": 14}]
        orgs = {10: "own", 11: "A", 12: "A", 13: "B", 14: None}
        n = boilerplate.count_sources([1, 2, 3, 4, 5, 6], sources, orgs, own_card=99, own_org="own")
        self.assertEqual(n, 4)   # A, B, экспертиза 500, карточка 14


class TypicalFragmentsTest(unittest.TestCase):
    """Поиск типовых цитат с заглушками эмбеддингов, хранилища и внешней БД."""

    def test_typical(self):
        """Цитата есть в документах трёх других заказчиков — 3; уникальная — не возвращается."""
        unique = "Участник обязан иметь собственный асфальтобетонный завод в Орловской области не далее 20 км от объекта"

        class Emb:
            async def get_embeddings(self, texts, strict=False):
                return [[0.1] * 4 for _ in texts]

        class Repo:
            async def find_chunks_all(self, vec, model, limit=30):
                return [{"document_id": d, "text": "раздел 3. " + FRAG} for d in (7, 8, 9, 42)]

            async def document_sources(self, env, ids):
                return [{"document_id": 7, "kind": "rm", "source_id": 70}, {"document_id": 8, "kind": "rm", "source_id": 80},
                        {"document_id": 9, "kind": "pe", "source_id": 900}, {"document_id": 42, "kind": "rm", "source_id": 1}]

        async def orgs(cards):
            return {70: "A", 80: "B", 1: "own"}

        out = asyncio.run(boilerplate.typical_fragments(
            [FRAG, unique], embedder=Emb(), repo=Repo(), env="dev", document_id=42, own_card=1, own_org="own",
            embedding_model="m", fetch_orgs=orgs))
        self.assertEqual(out, {FRAG: 3})


class NormsTest(unittest.TestCase):
    """Выжимка норм под тип документа."""

    def test_digest(self):
        """Требования к заявке — ст. 31, ПП 2571, СРО; 223-ФЗ — положение о закупке; версия меняется с текстом."""
        req = norms.digest("docTrebContentRequestFiles", 44)
        for part in ("ст. 31", "2571", "СРО", "19.28", "ст. 45"):
            self.assertIn(part, req)
        self.assertIn("положение о закупке", norms.digest("docTrebContentRequestFiles", 223))
        self.assertIn("ПП РФ № 1042", norms.digest("docProjContractFiles", 44))
        self.assertNotEqual(norms.version("docTrebContentRequestFiles", 44), norms.version("docProjContractFiles", 44))

    def test_in_user_message(self):
        """Выжимка идёт в запрос отдельным блоком, а не обрезается вместе с контекстом."""
        from risk_monitoring.document_risk_analyzer import DocumentRiskAnalyzer

        seen = {}

        async def create(**kw):
            """Модель-заглушка."""
            seen["user"] = kw["messages"][1]["content"]
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content='{"summary": "", "risks": []}'))])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        an = DocumentRiskAnalyzer(client, "m")
        ctx = {"summary": "x" * 5000, "norms": norms.digest("docTrebContentRequestFiles", 44)}
        asyncio.run(an._analyze_chunk("Текст документа", "f.docx", ctx, 1, 1, ["DOC-004"]))
        self.assertIn("Нормы, установленные законом", seen["user"])
        self.assertIn("ПП РФ от 29.12.2021 № 2571", seen["user"])


if __name__ == "__main__":
    unittest.main()


class EventCodesTest(unittest.TestCase):
    """XML 3209: на изменение извещения модель выставила CTR-005, CTR-006, PROC-001, PROC-002."""

    def test_event_codes(self):
        """Коды контракта к закупке и коды правил от модели — отбрасываются."""
        from risk_monitoring.xml_events import to_external_event_risks

        llm = [{"rule_id": c} for c in ("FIN-013", "CTR-006", "CTR-005", "PROC-002", "PROC-001", "PROC-003")]
        kept = [r["rule_id"] for r in to_external_event_risks(llm, {"group": "purchase"})]
        self.assertEqual(kept, ["FIN-013", "PROC-003"])
        kept = [r["rule_id"] for r in to_external_event_risks([{"rule_id": "CTR-005"}], {"group": "contract"})]
        self.assertEqual(kept, ["CTR-005"])

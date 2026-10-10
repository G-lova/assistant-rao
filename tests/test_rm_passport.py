"""Тесты сборки AI-паспорта документа, сравнения редакций и вспомогательных модулей риск-мониторинга."""
import asyncio
import unittest

import numpy as np

from configs.embedding_client import EmbeddingClient, EmbeddingError
from risk_monitoring import passport, rm_sql, version_diff
from risk_monitoring.llm_json import parse_json, prompt_version

PASSPORT_FIELDS = {"doc_type", "readability", "analyzed_at", "model", "resume", "confidence", "raw_data",
                   "total_doc_risk", "risks", "similar_doc_ids"}
RISK_FIELDS = {"rule_id", "title", "description", "level", "confidence", "law", "evidence", "verification_needed"}


def compact_risk(code="DOC-002", severity="high", fragment="модель X-100", conf=0.9, **kw):
    """Риск в формате ``DocumentRiskAnalyzer.to_compact``."""
    r = {"code": code, "category": code.split("-")[0], "severity": severity, "title": "t", "explanation": "e",
         "fragment": fragment, "section": "п. 1", "page": 2, "confidence": conf, "law": None, "verification_needed": ["v"]}
    r.update(kw)
    return r


class RisksTests(unittest.TestCase):
    """Внешний формат рисков, объединение, интегральный балл."""

    def test_external_format_fields(self):
        """Риск содержит все поля, которые читает паспорт; норма по умолчанию из каталога."""
        out = passport.to_external_risks([compact_risk()])
        self.assertEqual(len(out), 1)
        self.assertTrue(RISK_FIELDS <= set(out[0]), set(out[0]))
        self.assertEqual(out[0]["level"], 0.9)
        self.assertIn("ст. 33", out[0]["law"])
        self.assertTrue(out[0]["evidence"][0]["quote_verified"])

    def test_merge_same_code(self):
        """Фрагменты одного кода объединяются в один риск (уникальность во внешней БД по file_id + rule_id)."""
        out = passport.to_external_risks([compact_risk(fragment="a", severity="medium"),
                                          compact_risk(fragment="b", severity="high")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["level"], 0.9)
        self.assertEqual([e["fragment"] for e in out[0]["evidence"]], ["a", "b"])

    def test_total_risk_noisy_or(self):
        """Один серьёзный риск даёт высокий балл, пустые категории его не занижают."""
        one = passport.to_external_risks([compact_risk(conf=1.0)])
        self.assertAlmostEqual(passport.total_risk(one), 0.9)
        self.assertEqual(passport.total_risk([]), 0.0)
        two = passport.merge_risks(one, [{"rule_id": "FIN-005", "level": 0.6, "confidence": 1.0}])
        self.assertAlmostEqual(passport.total_risk(two), 1 - 0.1 * 0.4, places=4)

    def test_risk_profile(self):
        """Профиль по категориям: число, максимум, балл."""
        risks = passport.merge_risks([{"rule_id": "DOC-001", "level": 0.9, "confidence": 1},
                                      {"rule_id": "DOC-009", "level": 0.3, "confidence": 1},
                                      {"rule_id": "FIN-005", "level": 0.6, "confidence": 1}])
        prof = passport.risk_profile(risks)
        self.assertEqual(prof["DOC"]["count"], 2)
        self.assertEqual(prof["DOC"]["max_level"], 0.9)
        self.assertEqual(prof["FIN"]["count"], 1)

    def test_risks_comparison(self):
        """Сопоставление с предыдущей версией: сохранившиеся, новые, не выявленные."""
        cmp_ = passport.risks_comparison(["DOC-001", "DOC-009"], ["DOC-001", "FIN-005"])
        self.assertEqual(cmp_, {"persisting": ["DOC-001"], "new": ["FIN-005"], "not_found_in_new_version": ["DOC-009"]})
        self.assertIsNone(passport.risks_comparison(None, ["DOC-001"]))

    def test_clamp_percent(self):
        """Проценты приводятся к долям."""
        self.assertEqual(passport.clamp01(95), 0.95)
        self.assertEqual(passport.clamp01("0.5"), 0.5)
        self.assertIsNone(passport.clamp01("x"))


class PassportTests(unittest.TestCase):
    """Итоговый ``ai_analysis`` файла."""

    def build(self, **kw):
        """Паспорт с типовыми входами."""
        args = dict(
            file_meta={"id": 1097, "file_name": "ТЗ.pdf", "owner_type": "notice", "eis_version": 1},
            type_info={"detected_type": "docTZFiles", "confidence": 0.93},
            type_decode="Техническое задание",
            extraction={"readability": {"status": "allow", "readability_score": 0.9, "main_language": "ru"},
                        "raw_data": {"summary": "Кратко", "dates": []}},
            profile={"resume": "Заключение", "purpose": "ТЗ", "confidence": 0.8,
                     "technical_objects": {"brands": ["X"], "models": [], "manufacturers": [], "standards": ["ГОСТ 1"]},
                     "assessment": {"competition_restriction": 0.7}},
            risk_compact={"summary": "s", "risks": [compact_risk()]},
            extra_risks=[], similar=[{"document_id": 5, "external_file_ids": [1097, 1096], "avg_similarity": 0.97,
                                      "coverage": 0.95, "near_duplicate": True}],
            version_changes=None, previous_codes=["DOC-009"], pages=3, text_chars=1000, ocr_used=False,
            purchase={"purchase_number": "0372100039726000019"}, pipeline={"status": "completed"},
            model="m", content_sha256="abc")
        args.update(kw)
        return passport.build_file_analysis(**args)

    def test_has_passport_fields(self):
        """Все поля, которые читает интерфейс паспорта, на месте; эмбеддингов нет."""
        ai = self.build()
        self.assertTrue(PASSPORT_FIELDS <= set(ai), PASSPORT_FIELDS - set(ai))
        self.assertNotIn("embeddings", ai)
        self.assertEqual(ai["doc_type"]["type_decode"], "Техническое задание")
        self.assertEqual(ai["readability"]["pages"], 3)
        self.assertEqual(ai["resume"], "Заключение")
        self.assertEqual(ai["risks_comparison"]["not_found_in_new_version"], ["DOC-009"])
        self.assertEqual(ai["passport"]["technical_objects"]["brands"], ["X"])

    def test_similar_excludes_self(self):
        """Похожие документы — id файлов без самого файла."""
        ai = self.build()
        self.assertEqual([s["id"] for s in ai["similar_doc_ids"]], [1096])
        self.assertEqual(ai["similar_doc_ids"][0]["similarity"], 0.97)

    def test_resume_fallbacks(self):
        """Без профиля резюме берётся из извлечения."""
        ai = self.build(profile=None)
        self.assertEqual(ai["resume"], "Кратко")
        self.assertEqual(ai["confidence"], 0.93)

    def test_no_text(self):
        """Документ без текста — законченный результат с причиной."""
        ai = passport.no_text_analysis({"file_name": "скан.pdf"}, "Не удалось извлечь текст",
                                       passport.pipeline_block("S1b", "completed", "m", result="no_text"), "m")
        self.assertEqual(ai["pipeline"]["result"], "no_text")
        self.assertEqual(ai["risks"], [])
        self.assertTrue(PASSPORT_FIELDS <= set(ai))

    def test_pipeline_block(self):
        """Блок pipeline содержит версию схемы, каталог, статус, ссылку на предыдущую версию."""
        b = passport.pipeline_block("S1b", "completed", "m", {"risk": "x"}, previous_scope_id=7, debug=True)
        self.assertEqual(b["schema_version"], passport.SCHEMA_VERSION_FILE)
        self.assertEqual(b["previous_scope_id"], 7)
        self.assertTrue(b["debug"])


class VersionDiffTests(unittest.TestCase):
    """Сравнение редакций документа."""

    def test_identical(self):
        """Одинаковые тексты — ``identical``."""
        self.assertTrue(version_diff.text_diff("a\nb", "a\nb")["identical"])

    def test_changed_paragraph(self):
        """Изменённый абзац попадает во фрагменты, маркеры страниц игнорируются."""
        old = "Страница 1: Поставка оборудования.\nСрок поставки 30 дней."
        new = "Страница 1: Поставка оборудования производителя XXX.\nСрок поставки 30 дней."
        d = version_diff.text_diff(old, new)
        self.assertFalse(d["identical"])
        self.assertEqual(d["stats"]["replaced"], 1)
        self.assertIn("XXX", d["fragments"][0]["after"])
        self.assertGreater(d["similarity"], 0.3)

    def test_version_risk(self):
        """Риск DOC-011 — только если модель отметила изменения, повышающие риск."""
        llm = {"risk_relevant": True, "summary": "Появился производитель",
               "changes": [{"risk_effect": "increases", "significance": 0.8, "subject": "производитель",
                            "before": "Поставка оборудования", "after": "Поставка оборудования производителя XXX"}]}
        risks = version_diff.version_risks(llm, 10)
        self.assertEqual(risks[0]["rule_id"], "DOC-011")
        self.assertEqual(risks[0]["level"], 0.8)
        self.assertEqual(risks[0]["evidence"][0]["previous_file_id"], 10)
        self.assertEqual(version_diff.version_risks({"risk_relevant": False}, 10), [])


class HelpersTests(unittest.TestCase):
    """SQL-шаблоны, разбор JSON, клиент эмбеддингов."""

    def test_render_sql(self):
        """Маркеры списков раскрываются в плейсхолдеры, биндинги — в порядке появления."""
        sql, b = rm_sql.render("xml_files.sql", ids=[1, 2], urls=["u"])
        self.assertEqual(sql.count("?"), 3)
        self.assertEqual(b, [1, 2, "u"])
        sql, b = rm_sql.render("files_meta.sql", head=["https://s/"], ids=[5])
        self.assertEqual(sql.count("?"), 2)
        self.assertEqual(b, ["https://s/", 5])
        sql, b = rm_sql.render("link_by_number.sql", nums=[])
        self.assertIn("IN (?)", sql)
        self.assertEqual(b, [None])
        with self.assertRaises(ValueError):
            rm_sql.render("xml_files.sql", ids=[1])

    def test_all_rm_queries_render(self):
        """Все запросы ``queries/rm`` загружаются и раскрываются."""
        for path in rm_sql.QUERIES_DIR.glob("*.sql"):
            text = rm_sql.load(path.name)
            lists = {k: [1] for k in ("ids", "urls", "nums") if "{" + k + "}" in text}
            sql, _ = rm_sql.render(path.name, **lists)
            self.assertNotIn("{", sql.replace("{ids}", ""), path.name)

    def test_parse_json(self):
        """JSON из ответа модели: обычный, в ```json-блоке, с текстом вокруг."""
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(parse_json('Ответ: {"a": 3} конец'), {"a": 3})
        self.assertNotEqual(prompt_version("a"), prompt_version("b"))

    def test_embedding_strict(self):
        """Строгий режим: упавший пакет — исключение; мягкий — пакет пропускается."""
        client = EmbeddingClient("http://x", None, "m", batch_size=2, http_manager=None)

        async def fake(batch):
            """Второй пакет падает."""
            if batch[0] == "c":
                raise RuntimeError("boom")
            return [[1.0, 0.0] for _ in batch]

        client._get_batch = fake
        with self.assertRaises(EmbeddingError):
            asyncio.run(client.get_embeddings(["a", "b", "c"], strict=True))
        soft = asyncio.run(client.get_embeddings(["a", "b", "c"]))
        self.assertEqual(soft.shape, (2, 2))
        self.assertIsInstance(soft, np.ndarray)

    def test_embedding_formats(self):
        """Три формата ответа сервиса эмбеддингов; OpenAI-формат сортируется по index."""
        ex = EmbeddingClient.extract_embeddings
        self.assertEqual(ex({"embedding": [1]}), [[1]])
        self.assertEqual(ex({"embeddings": [[1], [2]]}), [[1], [2]])
        self.assertEqual(ex({"data": [{"index": 1, "embedding": [2]}, {"index": 0, "embedding": [1]}]}), [[1], [2]])
        self.assertIsNone(ex({"x": 1}))


if __name__ == "__main__":
    unittest.main()

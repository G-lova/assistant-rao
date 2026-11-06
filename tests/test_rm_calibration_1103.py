"""Калибровка балла закупки (03.11.2026): 227 из 237 закупок на stage были в красной зоне.

Проверяется:
1. скидка для рисков, найденных только моделью, работает и при упрощённой метке источника ``ai``;
2. CR-004 «Аванс без достаточного обеспечения» требует нарушения требований к обеспечению, а не только признака;
3. при объединении рисков уровень правила не повышается уровнем модели;
4. в документах исполнения (``contractProcedure``) риски содержания документации не учитываются;
5. FIN-005 (``pz_p2``) при закупке у единственного поставщика понижается без модели (исключение по способу).
"""
import unittest

from configs import settings
from risk_monitoring import composite_risks as cr
from risk_monitoring import contract_package as cp
from risk_monitoring import doc_kind, passport
from risk_monitoring import rule_exclusions as rx


def _r(code, level=0.8, conf=1.0, **kw):
    """Риск во внешнем формате."""
    return {"rule_id": code, "title": f"Риск {code}", "level": level, "confidence": conf, **kw}


class AiWeightTest(unittest.TestCase):
    """Скидка AI_UNCONFIRMED_WEIGHT для рисков модели."""

    def test_simple_label_ai_is_discounted(self):
        """sources = [ai_file, ai] (так их сохраняет merge_risks) — риск модели, вес × 0,6."""
        r = _r("DOC-002", 0.9, 0.95, sources=["ai_file", "ai"])
        w = cp._weight(r, (), settings.CONFIRMED_WEIGHT, settings.AI_UNCONFIRMED_WEIGHT)
        self.assertAlmostEqual(w, 0.9 * 0.95 * settings.AI_UNCONFIRMED_WEIGHT, places=4)

    def test_score_of_single_ai_risk_not_red(self):
        """Один неподтверждённый DOC-002 0,9 × 0,95 больше не даёт красную зону (было 85,5)."""
        prof = cp.profile([_r("DOC-002", 0.9, 0.95, sources=["ai_file", "ai"])], None, (),
                          settings.CONFIRMED_WEIGHT, tuple(settings.SCORE_ZONES), settings.SCORE_DECAY,
                          settings.AI_UNCONFIRMED_WEIGHT)
        self.assertLess(prof["score"], settings.SCORE_ZONES[1])

    def test_rule_with_ai_not_discounted(self):
        """Риск, подтверждённый правилом, скидки не получает."""
        r = _r("CTR-004", 0.5, 1.0, sources=["rule", "ai_event"])
        self.assertAlmostEqual(cp._weight(r, (), 1.5, 0.6), 0.5, places=4)

    def test_confirmed_ai_not_discounted(self):
        """Подтверждённый аналитиком риск модели весит × CONFIRMED_WEIGHT, без скидки."""
        r = _r("DOC-002", 0.5, 1.0, sources=["ai_file", "ai"])
        self.assertAlmostEqual(cp._weight(r, {"DOC-002"}, 1.5, 0.6), 0.75, places=4)


class Cr004Test(unittest.TestCase):
    """CR-004 «Аванс без достаточного обеспечения»."""

    def _codes(self, risks):
        """Коды сработавших составных рисков."""
        return {x["rule_id"] for x in cr.detect(risks)}

    def test_advance_with_min_security_marker_only(self):
        """Аванс (FIN-008) + признак «минимальное обеспечение» (FIN-005) — не составной риск: обеспечение по закону
        часто не требуется (ЕП, малые закупки)."""
        risks = [_r("FIN-008", settings.RULE_RISK_LEVEL, sources=["rule"]),
                 _r("FIN-005", settings.RULE_RISK_LEVEL, sources=["rule"])]
        self.assertNotIn("CR-004", self._codes(risks))

    def test_advance_with_security_violation(self):
        """Аванс + нарушение требований к обеспечению (FIN-007) — CR-004."""
        risks = [_r("FIN-008", settings.RULE_RISK_LEVEL, sources=["rule"]),
                 _r("FIN-007", settings.RULE_VIOLATION_LEVEL, sources=["rule"])]
        self.assertIn("CR-004", self._codes(risks))


class RuleLevelTest(unittest.TestCase):
    """Уровень правила при объединении с риском модели."""

    def test_rule_level_wins_over_model(self):
        """Событие: правило CTR-004 0,5 + модель 0,9 → 0,5; источники и цитаты обоих сохраняются."""
        rule = [_r("CTR-004", 0.5, 1.0, source="rule", evidence=[{"src": "rule"}])]
        model = [_r("CTR-004", 0.9, 0.8, source="ai_event", evidence=[{"src": "ai"}])]
        m = passport.merge_risks(rule, model)[0]
        self.assertEqual(m["level"], 0.5)
        self.assertEqual(m["confidence"], 1.0)
        self.assertEqual({e["src"] for e in m["evidence"]}, {"rule", "ai"})

    def test_model_only_keeps_max(self):
        """Правил по коду нет — уровень по модели (максимум)."""
        a = [_r("DOC-002", 0.6, 0.9, source="ai_file")]
        b = [_r("DOC-002", 0.8, 0.9, source="ai_file")]
        self.assertEqual(passport.merge_risks(a, b)[0]["level"], 0.8)

    def test_higher_rule_kept(self):
        """Правило выше модели — уровень правила."""
        rule = [_r("CTR-020", 0.8, 1.0, source="rule")]
        model = [_r("CTR-020", 0.6, 0.9, source="ai_event")]
        self.assertEqual(passport.merge_risks(rule, model)[0]["level"], 0.8)


class ExecutionDocumentTest(unittest.TestCase):
    """Документы исполнения контракта."""

    def test_is_execution_document(self):
        """contractProcedure — документ исполнения; контракт и извещение — нет; без тега — нет."""
        self.assertTrue(doc_kind.is_execution_document({"xml_source_type": "contractProcedure", "owner_type": "contract"}))
        self.assertFalse(doc_kind.is_execution_document({"xml_source_type": "contract", "owner_type": "contract"}))
        self.assertFalse(doc_kind.is_execution_document({"xml_source_type": "epNotification", "owner_type": "notice"}))
        self.assertFalse(doc_kind.is_execution_document({"owner_type": "contract"}))

    def test_file_risks_skip_execution_documents(self):
        """Риски модели в акте (contractProcedure) в закупку не идут; подтверждённый аналитиком — идёт."""
        files = [
            {"id": 1, "file_name": "Документ 1.pdf", "xml_source_type": "contractProcedure", "owner_type": "contract",
             "risks": [_r("DOC-007", 0.9, 0.95, evidence=[{"fragment": "сумма прописью"}])]},
            {"id": 2, "file_name": "Документ 2.pdf", "xml_source_type": "contractProcedure", "owner_type": "contract",
             "risks": [_r("DOC-006", 0.9, 0.95, evidence=[{"fragment": "срок"}])]},
            {"id": 3, "file_name": "Проект контракта.docx", "xml_source_type": "contract", "owner_type": "contract",
             # во вложении XML контракта риски документации закупки (DOC-002 …) не учитываются (06.11.2026) —
             # противоречие (DOC-007) учитывается
             "risks": [_r("DOC-007", 0.9, 0.95, evidence=[{"fragment": "срок 10 дней и срок 20 дней"}]),
                       _r("DOC-002", 0.9, 0.95, evidence=[{"fragment": "товарный знак"}])]},
        ]
        dropped = []
        risks, confirmed = cp.file_risks(files, {(2, "DOC-006"): cp.CONFIRMED_STATUS}, dropped)
        self.assertEqual({(r["rule_id"], r["evidence"][0]["src"]) for r in risks},
                         {("DOC-006", "file:2"), ("DOC-007", "file:3")})
        self.assertIn({"rule_id": "DOC-002", "file_id": 3, "reason": "post_award_document"}, dropped)
        self.assertEqual(confirmed, {"DOC-006"})


class Fin005SingleSupplierTest(unittest.TestCase):
    """Исключение FIN-005 при законном отсутствии обеспечения (ЕП, ч. 1 ст. 93 44-ФЗ)."""

    def test_single_supplier_by_code(self):
        """Код способа EP… и сработавший pz_p2 — исключение single_supplier_93 из паспорта (src = eis)."""
        out = rx.data_checks({"pz_p2": 1}, {"method_code": "EP44", "method": "Закупка у единственного поставщика"})
        self.assertEqual(out["pz_p2"]["status"], "excluded")
        self.assertEqual(out["pz_p2"]["exclusion_id"], "single_supplier_93")
        self.assertEqual(out["pz_p2"]["evidence"]["src"], "eis")

    def test_single_supplier_by_name(self):
        """Кода нет — по названию способа."""
        out = rx.data_checks({"pz_p2": "1"}, {"method": "Закупка у единственного поставщика (подрядчика, исполнителя)"})
        self.assertIn("pz_p2", out)

    def test_competitive_not_excluded(self):
        """Электронный аукцион и запрос котировок — исключения нет; показатель не сработал — тоже нет."""
        self.assertEqual(rx.data_checks({"pz_p2": 1}, {"method_code": "EA20", "method": "Электронный аукцион"}), {})
        self.assertEqual(rx.data_checks({"pz_p2": 1}, {"method_code": "ZK20", "method": "Запрос котировок"}), {})
        self.assertEqual(rx.data_checks({"pz_p2": 0}, {"method_code": "EP44"}), {})

    def test_apply_lowers_rule_risk(self):
        """Риск FIN-005 от правила pz_p2 понижается (уровень × RULE_EXCLUDED_LEVEL_FACTOR), ссылка на ЕИС добавлена."""
        risk = _r("FIN-005", settings.RULE_RISK_LEVEL, 1.0, sources=["rule"], evidence=[{"src": "rule:pz_p2"}])
        checks = rx.data_checks({"pz_p2": 1}, {"method_code": "EP44"})
        self.assertEqual(rx.apply_checks([risk], checks, settings), 1)
        self.assertAlmostEqual(risk["level"], round(settings.RULE_RISK_LEVEL * settings.RULE_EXCLUDED_LEVEL_FACTOR, 3))
        self.assertLessEqual(risk["confidence"], settings.RULE_EXCLUDED_CONFIDENCE)
        self.assertEqual(risk["rule_check"]["norm"], "ч. 1 ст. 93, ч. 2 ст. 96 44-ФЗ")


if __name__ == "__main__":
    unittest.main()

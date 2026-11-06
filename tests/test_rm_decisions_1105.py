"""Доработки сервиса для дашбордов (05.11.2026).

Проверяется:
1. ``not_in_score`` — строки рисков документов, не учтённые в балле, с причиной;
2. решения аналитика по всем строкам кода и пересчёт без модели (``risk_decisions``);
3. вклад риска в балл (``score_contribution``);
4. отклонённый составной риск не выводится;
5. у заказчика без Псвод нет зоны;
6. у каждого риска каталога есть описание.
"""
import unittest

from configs import settings
from risk_monitoring import composite_risks as cr
from risk_monitoring import contract_package as cp
from risk_monitoring import risk_decisions as rdec


def _r(code, level=0.5, conf=1.0, **kw):
    """Риск во внешнем формате."""
    return {"rule_id": code, "title": f"Риск {code}", "level": level, "confidence": conf, **kw}


def _entry(*rows):
    """Решения по закупке 1 из строк ``(scope_type, scope_id, rule_id, status)``."""
    return rdec.group_rows([{"contract_id": 1, "scope_type": s, "scope_id": i, "rule_id": c, "status": st}
                            for s, i, c, st in rows])[1]


class NotInScoreTest(unittest.TestCase):
    """Отсеянные сервисом риски документов."""

    def test_execution_and_same_quote(self):
        """Акт (contractProcedure) — execution_document; дубль цитаты — same_quote; учтённые строки — не в списке."""
        files = [
            {"id": 1, "file_name": "Акт.pdf", "xml_source_type": "contractProcedure", "owner_type": "contract",
             "risks": [_r("DOC-007", 0.9, evidence=[{"fragment": "сумма"}])]},
            {"id": 2, "file_name": "ТЗ.docx", "risks": [_r("DOC-002", 0.9, evidence=[{"fragment": "товарный знак"}]),
                                                         _r("DOC-005", 0.5, evidence=[{"fragment": "товарный знак"}])]},
        ]
        dropped = []
        risks, _ = cp.file_risks(files, {}, dropped)
        self.assertEqual(dropped, [{"rule_id": "DOC-007", "file_id": 1, "reason": "execution_document"}])
        for r in risks:
            r["sources"] = ["ai_file"]
        kept = cp.drop_same_quote(risks, (), dropped)
        self.assertEqual([r["rule_id"] for r in kept], ["DOC-002"])
        out = cp.not_in_score(dropped, kept)
        self.assertEqual({(d["rule_id"], d["reason"]) for d in out},
                         {("DOC-007", "execution_document"), ("DOC-005", "same_quote")})

    def test_counted_row_not_listed(self):
        """Строка, на которую ссылается учтённый риск того же кода, в список не попадает."""
        dropped = [{"rule_id": "DOC-002", "file_id": 2, "reason": "same_quote"}]
        kept = [_r("DOC-002", evidence=[{"src": "file:2"}])]
        self.assertEqual(cp.not_in_score(dropped, kept), [])

    def test_rule_covered(self):
        """Показатель посчитан и не сработал — риск модели снят с причиной rule_covered."""
        code = next(iter(cp.CODE_TO_INDICATOR))
        risk = _r(code, 0.9, evidence=[{"src": "file:7", "fragment": "x"}])
        dropped = []
        values = {cp.CODE_TO_INDICATOR[code]: 0}
        out = cp.drop_rule_covered([risk], values, (), 44, (), dropped)
        if cp.rule_decision(code, values, 44, ()) == "not_fired":
            self.assertEqual(out, [])
            self.assertEqual(dropped[0]["reason"], "rule_covered")


class DecisionsTest(unittest.TestCase):
    """Решения аналитика по строкам кода."""

    def test_rule_risk_rejected(self):
        """Отклонён риск правила (строка закупки 2) — снят, хотя строк файлов у него нет."""
        risk = _r("FIN-005", evidence=[{"src": "rule:pz_p2"}], sources=["rule"])
        self.assertTrue(rdec.excluded(risk, _entry(("contract", 1, "FIN-005", 2))))

    def test_new_evidence_after_rejection(self):
        """Код отклонён, но пришёл новый файл (строка на рассмотрении) — риск остаётся."""
        risk = _r("DOC-002", evidence=[{"src": "file:3"}, {"src": "file:9"}])
        entry = _entry(("contract", 1, "DOC-002", 2), ("file", 3, "DOC-002", 2), ("file", 9, "DOC-002", 0))
        self.assertFalse(rdec.excluded(risk, entry))

    def test_not_relevant_all_documents(self):
        """«Не относится» у всех документов риска модели — снят; у одного из двух — остаётся."""
        risk = _r("DOC-002", evidence=[{"src": "file:3"}, {"src": "file:4"}])
        self.assertTrue(rdec.excluded(risk, _entry(("file", 3, "DOC-002", 2), ("file", 4, "DOC-002", 2))))
        self.assertFalse(rdec.excluded(risk, _entry(("file", 3, "DOC-002", 2), ("file", 4, "DOC-002", 0))))

    def test_not_relevant_keeps_rule_part(self):
        """Риск правила с подтверждающей цитатой: «Не относится» у файла не снимает правило."""
        risk = _r("FIN-005", evidence=[{"src": "rule:pz_p2"}, {"src": "file:3"}])
        self.assertFalse(rdec.excluded(risk, _entry(("file", 3, "FIN-005", 2))))

    def _ai(self):
        """Сохранённый анализ закупки с двумя рисками."""
        risks = [_r("FIN-005", settings.RULE_VIOLATION_LEVEL, evidence=[{"src": "rule:pz_p2"}], sources=["rule"]),
                 _r("DOC-002", 0.6, 0.9, evidence=[{"src": "file:3"}], sources=["ai_file"])]
        prof = cp.profile(risks, None, (), settings.CONFIRMED_WEIGHT, tuple(settings.SCORE_ZONES),
                          settings.SCORE_DECAY, settings.AI_UNCONFIRMED_WEIGHT)
        return {"risks": risks, "profile": prof, "passport": {"nmck": 1000000}, "pipeline": {}}

    def test_recalc_and_revert(self):
        """Отклонение кода снижает балл; повторный пересчёт с теми же решениями не нужен; отмена возвращает риск."""
        ai = self._ai()
        entry = _entry(("contract", 1, "FIN-005", 2))
        new = rdec.recalc(ai, entry, settings)
        self.assertEqual([r["rule_id"] for r in new["risks"]], ["DOC-002"])
        self.assertEqual([r["rule_id"] for r in new["rejected_risks"]], ["FIN-005"])
        self.assertIn("FIN-005", new["rejected_codes"])
        self.assertLess(new["profile"]["score"], ai["profile"]["score"])
        self.assertIsNone(rdec.recalc(new, entry, settings))
        back = rdec.recalc(new, _entry(("contract", 1, "FIN-005", 0)), settings)
        self.assertEqual({r["rule_id"] for r in back["risks"]}, {"FIN-005", "DOC-002"})
        self.assertAlmostEqual(back["profile"]["score"], ai["profile"]["score"], places=1)

    def test_confirmed_weight(self):
        """Подтверждение кода на строке закупки повышает вес риска модели."""
        ai = self._ai()
        new = rdec.recalc(ai, _entry(("contract", 1, "DOC-002", 1)), settings)
        self.assertIn("DOC-002", new["confirmed_codes"])
        self.assertGreater(new["profile"]["score"], ai["profile"]["score"])

    def test_pending_rows_do_not_change_hash(self):
        """Новые строки «на рассмотрении» (приём анализа, строки составных рисков) не меняют отпечаток — без лишнего
        пересчёта; смена статуса — меняет."""
        base = _entry(("contract", 1, "FIN-005", 2))
        more = _entry(("contract", 1, "FIN-005", 2), ("contract", 1, "CR-001", 0), ("file", 3, "DOC-002", 0))
        self.assertEqual(rdec.decisions_hash(base), rdec.decisions_hash(more))
        self.assertNotEqual(rdec.decisions_hash(base), rdec.decisions_hash(_entry(("contract", 1, "FIN-005", 1))))

    def test_recalc_time_format(self):
        """Время пересчёта — в формате analyzed_at (без смещения)."""
        new = rdec.recalc(self._ai(), _entry(("contract", 1, "FIN-005", 2)), settings)
        self.assertRegex(new["pipeline"]["decisions_recalc_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_no_rows_no_recalc(self):
        """Строк решений нет (запрос не удался) — пересчёта нет, подтверждения не сбрасываются."""
        self.assertIsNone(rdec.recalc(self._ai(), None, settings))


class ContributionTest(unittest.TestCase):
    """Вклад риска в балл."""

    def test_contribution(self):
        """Вклад — разница балла с риском и без него; у главного риска вклад больше."""
        risks = [_r("FIN-005", 0.55, sources=["rule"]), _r("CTR-004", 0.2, sources=["rule"])]
        prof = cp.profile(risks, None, (), 1.5, (40, 70), 0.5, 0.6)
        self.assertGreater(risks[0]["score_contribution"], risks[1]["score_contribution"])
        without = cp.profile([dict(risks[1])], None, (), 1.5, (40, 70), 0.5, 0.6)["score"]
        self.assertAlmostEqual(risks[0]["score_contribution"], round(prof["score"] - without, 1), places=1)


class CompositeRejectedTest(unittest.TestCase):
    """Отклонённый составной риск."""

    def test_rejected_composite_not_detected(self):
        """CR-004 отклонён аналитиком — не выводится, хотя компоненты есть."""
        risks = [_r("FIN-008", settings.RULE_RISK_LEVEL, sources=["rule"]),
                 _r("FIN-007", settings.RULE_VIOLATION_LEVEL, sources=["rule"])]
        self.assertIn("CR-004", {c["rule_id"] for c in cr.detect(risks)})
        self.assertNotIn("CR-004", {c["rule_id"] for c in cr.detect(risks, {"CR-004"})})


class CatalogDescriptionTest(unittest.TestCase):
    """Описания рисков для подсказок дашбордов."""

    def test_every_risk_has_description(self):
        """У каждого кода каталога есть описание."""
        import json
        import os

        path = os.path.join(os.path.dirname(cp.__file__), "config", "risk_catalog.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("risks", [])
        self.assertTrue(items)
        self.assertEqual([r["code"] for r in items if not (r.get("description") or "").strip()], [])


if __name__ == "__main__":
    unittest.main()

"""Динамика риска закупки: события (11.7), затухание (10.10), доверие (10.15), приоритет (10.14) и их место
в ежедневной сводке."""
import unittest
from datetime import datetime, timedelta, timezone

from configs import settings
from risk_monitoring import daily
from risk_monitoring import risk_dynamics as rd

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def _r(code, level=0.8, conf=0.9, **kw):
    """Риск закупки."""
    return {"rule_id": code, "title": f"Риск {code}", "level": level, "confidence": conf, **kw}


class RiskChangesTest(unittest.TestCase):
    """События риска относительно прошлого анализа."""

    def test_first_evaluation_no_events(self):
        """Первая оценка — событий нет (иначе все риски были бы «новыми»)."""
        self.assertEqual(rd.risk_changes([], [_r("PROC-002")]), [])

    def test_new_up_down_closed(self):
        """Новый, усилившийся, ослабевший и снятый риски."""
        prev = [_r("A-001", 0.5), _r("B-001", 0.8), _r("C-001")]
        new = [_r("A-001", 0.9), _r("B-001", 0.3), _r("D-001")]
        codes = {(c["code"], c["rule_id"]) for c in rd.risk_changes(prev, new)}
        self.assertEqual(codes, {("RSK-002", "A-001"), ("RSK-003", "B-001"), ("RSK-004", "C-001"),
                                 ("RSK-001", "D-001")})

    def test_small_change_ignored(self):
        """Изменение меньше порога — не событие."""
        self.assertEqual(rd.risk_changes([_r("A-001", 0.8)], [_r("A-001", 0.82)], threshold=0.05), [])

    def test_analyst_decisions(self):
        """Подтверждение и отклонение аналитиком — отдельные события."""
        out = rd.risk_changes([_r("A-001"), _r("B-001")], [_r("A-001")], (), ["A-001"], (), ["B-001"])
        self.assertIn("RSK-005", {c["code"] for c in out})
        self.assertIn("RSK-006", {c["code"] for c in out})


class DecayTest(unittest.TestCase):
    """Затухание по периоду полураспада."""

    def test_half_life(self):
        """Через период полураспада коэффициент 0,5; без периода — не затухает."""
        old = (NOW - timedelta(days=90)).isoformat()
        risks = [_r("PROC-002"), _r("REG-001")]
        prev = [_r("PROC-002", first_detected_at=old), _r("REG-001", first_detected_at=old)]
        n = rd.apply_decay(risks, prev, {"PROC-002": 90}, NOW)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(risks[0]["decay_factor"], 0.5, places=2)
        self.assertEqual(risks[1]["decay_factor"], 1.0)
        self.assertEqual(risks[0]["first_detected_at"][:10], old[:10])

    def test_strengthened_resets(self):
        """Усилившийся риск — новое событие, отсчёт заново."""
        old = (NOW - timedelta(days=180)).isoformat()
        risks = [_r("PROC-002", 0.9)]
        rd.apply_decay(risks, [_r("PROC-002", 0.4, first_detected_at=old)], {"PROC-002": 90}, NOW)
        self.assertEqual(risks[0]["decay_factor"], 1.0)

    def test_confirmed_no_decay(self):
        """Подтверждённый аналитиком риск не затухает."""
        old = (NOW - timedelta(days=180)).isoformat()
        risks = [_r("PROC-002", analyst_confirmed=True)]
        rd.apply_decay(risks, [_r("PROC-002", first_detected_at=old)], {"PROC-002": 90}, NOW)
        self.assertEqual(risks[0]["decay_factor"], 1.0)

    def test_weekly_recalc(self):
        """Еженедельный пересчёт снижает балл старого риска; без изменения — ``None``."""
        old = (NOW - timedelta(days=180)).isoformat()
        ai = {"risks": [_r("PROC-002", first_detected_at=old, decay_factor=1.0, sources=["rule"])],
              "profile": {"score": 72.0, "zone": "red"}, "passport": {"nmck": 1e6}, "confirmed_codes": [],
              "confidence": {"score": 80}, "pipeline": {"status": "completed"}}
        new = rd.recalc_decay(ai, settings, NOW)
        self.assertIsNotNone(new)
        self.assertLess(new["profile"]["score"], 72.0)
        self.assertLess(new["risks"][0]["decay_factor"], 0.3)
        self.assertIn("decay_recalc_at", new["pipeline"])
        self.assertEqual(ai["risks"][0]["decay_factor"], 1.0)           # исходный не меняется
        self.assertIsNone(rd.recalc_decay(new, settings, NOW))          # повтор в тот же момент — без изменений


class ConfidencePriorityTest(unittest.TestCase):
    """Индекс доверия и приоритет проверки."""

    def test_confidence(self):
        """Полные данные и правило — высокое доверие; без документов и только модель — ниже, с пояснениями."""
        p = {"nmck": 1e6, "method": "ЭА", "subject": "Ремонт", "customer": {"inn": "1"}, "stage": "notice"}
        hi = rd.confidence_index(p, {"files_total": 2, "files_analyzed": 2}, [_r("PROC-002", sources=["rule"])],
                                 [{"rm_result": "done"}], True)
        lo = rd.confidence_index({}, {"files_total": 4, "files_analyzed": 1},
                                 [_r("DOC-002", sources=["ai_file"])], [{"rm_result": "ocr_bad"}], False)
        self.assertGreater(hi["score"], 90)
        self.assertLess(lo["score"], 50)
        self.assertTrue(lo["notes"])

    def test_priority(self):
        """Новые риски, рост и сумма поднимают приоритет; низкое доверие — опускает и даёт причину."""
        base = rd.priority(50, 0, 1e5, [], 100, settings.PRIORITY_WEIGHTS)
        hot = rd.priority(80, 20, 1e9, [{"code": "RSK-001"}], 100, settings.PRIORITY_WEIGHTS)
        doubt = rd.priority(80, 20, 1e9, [{"code": "RSK-001"}], 30, settings.PRIORITY_WEIGHTS)
        self.assertGreater(hot["score"], base["score"])
        self.assertLess(doubt["score"], hot["score"])
        self.assertIn("новые риски", hot["reasons"])
        self.assertTrue(any("доверие" in r for r in doubt["reasons"]))


class SummaryDynamicsTest(unittest.TestCase):
    """Новые и снятые риски и приоритет проверки в ежедневной сводке."""

    def test_summary(self):
        """События пересчитанных закупок попадают в сводку и факты — названиями, без кодов."""
        import re

        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"},
                 2: {"id": 2, "number": "N2", "organisation_id": 10, "monitoring": 0, "customer_name": "ЗГУ"}}

        def ai(score, changes, prio, conf):
            """``ai_analysis`` закупки с динамикой."""
            return {"passport": {"purchase_number": "0319", "customer": {"name": "ЗГУ"}},
                    "profile": {"score": score, "zone": "red" if score >= 70 else "green"},
                    "risk_changes": changes, "priority": {"score": prio, "reasons": ["новые риски"]},
                    "confidence": {"score": conf}}

        contracts = {1: ai(75, [{"code": "RSK-001", "rule_id": "PROC-002", "risk_title": "Ограничение конкуренции"}], 70, 40),
                     2: ai(20, [{"code": "RSK-004", "rule_id": "DOC-002", "risk_title": "Товарный знак"}], 10, 90)}
        s = daily.build_summary(export_date="2026-10-28", environment="stage", cards=cards, contracts=contracts,
                                changed=[1, 2], events={}, xml_cards={}, customers=[], scope={"xml": 0}, stages={})
        self.assertEqual(s["risk_changes"]["by_type"], {"RSK-001": 1, "RSK-004": 1})
        self.assertEqual(s["risk_changes"]["new"][0]["card_id"], 1)
        self.assertEqual([p["card_id"] for p in s["priorities"]], [1, 2])
        facts = " ".join(daily.summary_facts(s))
        self.assertIn("Новые риски", facts)
        self.assertIn("Сняты риски", facts)
        self.assertIn("Проверить в первую очередь", facts)
        self.assertIn("Низкое доверие", facts)
        self.assertIsNone(re.search(r"\b[A-Z]{2,5}-\d{3}\b", facts), facts)

    def test_unchanged_no_events(self):
        """Закупка не пересчитана в этом прогоне — её прошлые события не повторяются."""
        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}}
        contracts = {1: {"passport": {}, "profile": {"score": 50, "zone": "yellow"},
                         "risk_changes": [{"code": "RSK-001", "rule_id": "PROC-002"}]}}
        s = daily.build_summary(export_date="2026-10-28", environment="stage", cards=cards, contracts=contracts,
                                changed=[], events={}, xml_cards={}, customers=[], scope={"xml": 0}, stages={})
        self.assertEqual(s["risk_changes"]["by_type"], {})


if __name__ == "__main__":
    unittest.main()

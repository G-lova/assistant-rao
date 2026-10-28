"""Составные риски закупки (``risk_monitoring.composite_risks``)."""
import re
import unittest

from configs import settings
from risk_monitoring import composite_risks as cr
from risk_monitoring import daily
from risk_monitoring import risk_dynamics as rd


def _r(code, level=0.8, conf=0.9, **kw):
    """Риск закупки."""
    return {"rule_id": code, "title": f"Риск {code}", "level": level, "confidence": conf, **kw}


class DetectTest(unittest.TestCase):
    """Шаблоны сочетаний."""

    def test_config_valid(self):
        """Коды шаблонов уникальны, коды факторов есть в каталоге."""
        from risk_monitoring import risk_catalog

        cfg = cr.load_config()
        codes = [p["code"] for p in cfg["patterns"]]
        self.assertEqual(len(codes), len(set(codes)))
        for p in cfg["patterns"]:
            for c in [x for g in p.get("all_of") or [] for x in g] + list(p.get("any_of") or []):
                self.assertIn(c, risk_catalog.RISKS, f"{p['code']}: {c}")

    def test_all_of(self):
        """Требование «под производителя» + одна заявка → CR-001 с факторами; одной группы мало."""
        out = cr.detect([_r("DOC-002"), _r("PROC-001", 0.7, 1.0)])
        cr1 = [c for c in out if c["rule_id"] == "CR-001"][0]
        self.assertEqual({x["rule_id"] for x in cr1["components"]}, {"DOC-002", "PROC-001"})
        self.assertAlmostEqual(cr1["confidence"], 0.95)
        self.assertEqual(cr1["kind"], "composite")
        self.assertEqual(cr.detect([_r("DOC-002")]), [])

    def test_any_of_min_count(self):
        """Цепочка изменений — от трёх разных изменений."""
        self.assertEqual([c["rule_id"] for c in cr.detect([_r("CTR-004"), _r("CTR-005")])], [])
        self.assertIn("CR-010", [c["rule_id"] for c in cr.detect([_r("CTR-004"), _r("CTR-005"), _r("CTR-014")])])

    def test_weak_excluded_rejected(self):
        """Слабый фактор, исключение по документам и отклонённый аналитиком не складываются в сочетание."""
        self.assertEqual(cr.detect([_r("FIN-003", 0.2, 0.5), _r("CTR-004")]), [])
        self.assertEqual(cr.detect([_r("FIN-003", rule_check={"status": "excluded"}), _r("CTR-004")]), [])
        self.assertEqual(cr.detect([_r("FIN-003"), _r("CTR-004")], rejected=["CTR-004"]), [])

    def test_decay_and_first_detected(self):
        """Затухание — минимальное по факторам; дата первого выявления переносится."""
        out = cr.detect([_r("FIN-003", decay_factor=0.5), _r("CTR-004")],
                        prev=[{"rule_id": "CR-002", "first_detected_at": "2026-01-01T00:00:00+00:00"}])
        self.assertEqual(out[0]["decay_factor"], 0.5)
        self.assertEqual(out[0]["first_detected_at"][:10], "2026-01-01")


class ScoreTest(unittest.TestCase):
    """Составной риск поднимает балл и попадает в сводку названием."""

    def test_rescore_adds(self):
        """Балл с составным риском выше, чем без него."""
        from risk_monitoring.contract_analyzer import rescore

        risks = [_r("FIN-003", 0.6, 0.8), _r("CTR-004", 0.6, 0.8)]
        a = {"risks": [dict(r) for r in risks], "profile": {}, "confirmed_codes": []}
        b = {"risks": [dict(r) for r in risks], "profile": {}, "confirmed_codes": [],
             "composite_risks": cr.detect(risks)}
        rescore(a, settings)
        rescore(b, settings)
        self.assertGreater(b["profile"]["score"], a["profile"]["score"])

    def test_new_composite_is_event(self):
        """Появившееся сочетание — событие «новый риск»."""
        prev = [_r("FIN-003")]
        new = [_r("FIN-003"), _r("CTR-004")]
        ch = rd.risk_changes(prev, new + cr.detect(new))
        self.assertIn(("RSK-001", "CR-002"), {(c["code"], c["rule_id"]) for c in ch})

    def test_summary_fact(self):
        """В фактах сводки — название сочетания, без кода."""
        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}}
        contracts = {1: {"passport": {"purchase_number": "0319"}, "profile": {"score": 70, "zone": "red"},
                         "composite_risks": [{"rule_id": "CR-002", "title": "Демпинг с последующим увеличением цены"}]}}
        s = daily.build_summary(export_date="2026-10-28", environment="stage", cards=cards, contracts=contracts,
                                changed=[1], events={}, xml_cards={}, customers=[], scope={"xml": 0}, stages={})
        self.assertEqual(s["composite_risks"], {"CR-002": 1})
        facts = " ".join(daily.summary_facts(s))
        self.assertIn("демпинг с последующим увеличением цены — закупок 1", facts.lower())
        self.assertIsNone(re.search(r"\bCR-\d{3}\b", facts), facts)


if __name__ == "__main__":
    unittest.main()

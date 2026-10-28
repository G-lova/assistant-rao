"""Профили закупки: исполнение контракта и сравнение с похожими закупками (``risk_monitoring.purchase_profiles``)."""
import unittest
from datetime import date

from configs import settings
from risk_monitoring import daily
from risk_monitoring import purchase_profiles as pp

AUCTION = {"method_code": "EAP20", "method": "Электронный аукцион"}


def _ds(**kw):
    """``dataset`` контракта."""
    d = {"contract_details": {"num_contr": "1", "contr_status": kw.get("status", "Исполнение")},
         "ispolnenie": {"contr_zena": "1000", "d_contr_plan": kw.get("plan", "2026-06-30+03:00"),
                        "d_contr_fact": kw.get("fact"), "contr_srok_ispoln_days": kw.get("plan_days"),
                        "contr_srok_fact_days": kw.get("fact_days"), "contr_zena_ispoln": kw.get("executed")},
         "contract": {"stages_count": kw.get("stages", "1")},
         "procedure": {"paid_sum": kw.get("paid"), "final_stage": kw.get("final_stage")}}
    return d


class ExecutionTest(unittest.TestCase):
    """Профиль исполнения и его риски."""

    def test_no_contract(self):
        """Контракта нет — профиля нет."""
        self.assertIsNone(pp.execution_profile({"izvejenie": {"nmck": "1"}}))

    def test_delay(self):
        """Просрочка однозтапного контракта → CTR-017 с уровнем по числу дней."""
        ex = pp.execution_profile(_ds(plan_days="100", fact_days="140", fact="2026-08-01"), date(2026, 10, 28))
        self.assertEqual(ex["delay_days"], 40)
        r = pp.execution_risks(ex, settings)
        self.assertEqual([x["rule_id"] for x in r], ["CTR-017"])
        self.assertEqual(r[0]["level"], 0.6)

    def test_multistage_no_delay(self):
        """У многоэтапного контракта фактический срок — срок этапа: просрочку не считаем."""
        ex = pp.execution_profile(_ds(plan_days="100", fact_days="140", stages="12"), date(2026, 10, 28))
        self.assertIsNone(ex["delay_days"])
        self.assertEqual(pp.execution_risks(ex, settings), [])

    def test_underdone_only_completed(self):
        """Недоисполнение — только при статусе «завершено», а не при завершённом этапе."""
        stage = pp.execution_profile(_ds(executed="500", final_stage="true", fact="2026-05-01"), date(2026, 10, 28))
        self.assertFalse(stage["completed"])
        self.assertEqual(pp.execution_risks(stage, settings), [])
        done = pp.execution_profile(_ds(executed="500", status="Исполнение завершено"), date(2026, 10, 28))
        self.assertEqual([x["rule_id"] for x in pp.execution_risks(done, settings)], ["CTR-018"])

    def test_plan_passed_open(self):
        """Срок прошёл, сведений о завершении нет — пометка, а не риск."""
        ex = pp.execution_profile(_ds(), date(2026, 10, 28))
        self.assertTrue(ex["plan_passed_open"])
        self.assertTrue(ex["notes"])
        self.assertEqual(pp.execution_risks(ex, settings), [])


class BenchmarkTest(unittest.TestCase):
    """Сравнение снижения цены с похожими закупками."""

    def setUp(self):
        """12 похожих аукционов класса 32.50 со снижением 20–31 %."""
        self.peers = [{"id": i, "fz_type": 44, "okpd2": "32.50.13.190", "discount": str(20 + i), "applications": "3",
                       **AUCTION} for i in range(12)]

    def passport(self, disc, apps=3, okpd="32.50"):
        """Паспорт сравниваемой закупки."""
        return {"card_id": 99, "okpd2": okpd, "fz": 44, "winner_discount_pct": disc, "applications": apps, **AUCTION}

    def test_low_discount(self):
        """Снижение 1 % при трёх заявках и медиане ≈ 25 % → FIN-001."""
        p = self.passport(1)
        b = pp.benchmark(p, self.peers, settings)
        self.assertEqual((b["group"], b["peers"]), ("class", 12))
        r = pp.benchmark_risks(b, p, settings)
        self.assertEqual(r[0]["rule_id"], "FIN-001")
        self.assertIn("3 заявках", r[0]["description"])

    def test_single_application_not_doubled(self):
        """Одна заявка — уже «низкая конкуренция»: FIN-001 не добавляется."""
        p = self.passport(0, apps=1)
        self.assertEqual(pp.benchmark_risks(pp.benchmark(p, self.peers, settings), p, settings), [])

    def test_normal_and_few_peers(self):
        """Обычное снижение — без риска; мало похожих — сравнения нет; себя не сравниваем."""
        p = self.passport(24)
        self.assertEqual(pp.benchmark_risks(pp.benchmark(p, self.peers, settings), p, settings), [])
        self.assertIsNone(pp.benchmark(self.passport(1, okpd="01.11"), self.peers, settings))
        own = dict(self.peers[0], id=99)
        self.assertEqual(pp.benchmark(p, self.peers[1:] + [own], settings)["peers"], 11)

    def test_division_fallback(self):
        """Мало похожих в классе — раздел ОКПД2, уровень ниже."""
        p = self.passport(1, okpd="32.99")
        b = pp.benchmark(p, self.peers, settings)
        self.assertEqual((b["group"], b["okpd2"]), ("division", "32"))
        self.assertLess(pp.benchmark_risks(b, p, settings)[0]["level"], settings.BENCHMARK_RISK_LEVEL)


class SummaryExecutionTest(unittest.TestCase):
    """Контракты с прошедшим сроком без сведений о завершении — факт сводки."""

    def test_fact(self):
        """Счётчик и фраза."""
        cards = {1: {"id": 1, "number": "N1", "organisation_id": 10, "monitoring": 1, "customer_name": "ЗГУ"}}
        contracts = {1: {"passport": {}, "profile": {"score": 30, "zone": "green"},
                         "execution": {"plan_passed_open": True}}}
        s = daily.build_summary(export_date="2026-10-28", environment="stage", cards=cards, contracts=contracts,
                                changed=[1], events={}, xml_cards={}, customers=[], scope={"xml": 0}, stages={})
        self.assertEqual(s["execution_open_past_plan"], 1)
        self.assertIn("сведений о завершении в ЕИС нет", " ".join(daily.summary_facts(s)))


if __name__ == "__main__":
    unittest.main()

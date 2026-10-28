"""Сводки за неделю и месяц (``risk_monitoring.period_summary``) и жалобы в профилях организаций."""
import asyncio
import unittest
from datetime import date

from configs import settings
from risk_monitoring import org_profile
from risk_monitoring import period_summary as ps
from risk_monitoring.xml_events import complaint_facts


def _daily(d, status="completed", events=3, new=None, red=None, prio=None):
    """Ежедневная сводка за дату."""
    return {"date": d, "pipeline": {"status": status, "finished_at": d + "T23:00:00"},
            "events": {"total": events, "by_type": {"Жалоба": 1, "Размещено извещение": events - 1},
                       "important": [{"xml_id": 1, "title": "Жалоба", "risks": ["PROC-008"]}]},
            "contracts": {"changed": 2, "became_red": red or [], "score_jump": []},
            "risk_changes": {"by_type": {"RSK-001": len(new or [])}, "new": new or [], "closed": []},
            "composite_risks": {"CR-002": 1}, "priorities": prio or [], "customers": [], "attention": [],
            "execution_open_past_plan": 4}


class PeriodTest(unittest.TestCase):
    """Границы периодов и сборка."""

    def test_bounds(self):
        """ISO-неделя и календарный месяц; ключи и прошлые периоды."""
        self.assertEqual(ps.period_bounds("week", "2026-W44"), (date(2026, 10, 26), date(2026, 11, 1)))
        self.assertEqual(ps.period_bounds("month", "2026-12"), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(ps.period_key("week", "2026-W44"), 202644)
        self.assertEqual(ps.previous_period("week", "2026-W01"), "2025-W52")
        self.assertEqual(ps.last_closed("week", date(2026, 10, 28)), "2026-W43")
        self.assertEqual(ps.last_closed("month", date(2026, 10, 28)), "2026-09")
        with self.assertRaises(ValueError):
            ps.period_bounds("year", "2026")

    def test_build(self):
        """Суммы за дни, пропуски, последняя оценка закупки, сравнение с прошлым периодом, факты без кодов."""
        import re

        red = [{"card_id": 5, "purchase_number": "N5", "customer": "ЗГУ", "score": 80}]
        new = [{"card_id": 5, "purchase_number": "N5", "rule_id": "FIN-001", "title": "Аномальная цена"}]
        dailies = {"2026-10-19": _daily("2026-10-19", red=red, new=new,
                                        prio=[{"card_id": 5, "purchase_number": "N5", "customer": "ЗГУ", "priority": 50}]),
                   "2026-10-20": _daily("2026-10-20", status="partial",
                                        prio=[{"card_id": 5, "purchase_number": "N5", "customer": "ЗГУ", "priority": 70}])}
        prev = {"period": {"period": "2026-W42"}, "events": {"total": 10}, "contracts": {"became_red": 0},
                "risk_changes": {"new_total": 3}}
        s = ps.build_period_summary("week", "2026-W43", dailies, prev, today=date(2026, 10, 28))
        self.assertEqual(s["events"]["total"], 6)
        self.assertEqual(s["days"]["partial"], ["2026-10-20"])
        self.assertEqual(len(s["days"]["missing"]), 5)
        self.assertEqual(s["priorities"][0]["priority"], 70)
        self.assertEqual(s["contracts"]["became_red"], 1)
        self.assertEqual(s["composite_risks"], {"CR-002": 2})
        self.assertEqual(s["comparison"]["events"], -4)
        facts = " ".join(ps.period_facts(s))
        self.assertIn("Сводка неполная", facts)
        self.assertIn("меньше на 4", facts)
        self.assertIsNone(re.search(r"\b[A-Z]{2,5}-\d{3}\b", facts), facts)

    def test_run_unchanged(self):
        """Повторный запуск без изменений ежедневных сводок — сохранённая сводка, без пересборки."""
        from risk_monitoring import daily

        class Repo:
            """Хранилище в памяти."""

            def __init__(self):
                """Одна ежедневная сводка."""
                self.data = {(daily.KIND, daily.run_key("2026-10-19")): {"ai_analysis": _daily("2026-10-19")}}
                self.saved = 0

            async def get_rm_results(self, env, kind, ids):
                """Результаты по id."""
                return {i: self.data[(kind, i)] for i in ids if (kind, i) in self.data}

            async def save_rm_result(self, env, kind, eid, ai, pushed, err):
                """Сохранение."""
                self.saved += 1
                self.data[(kind, eid)] = {"ai_analysis": ai}

        repo = Repo()
        s1 = asyncio.run(ps.run_period(repo, "stage", "week", "2026-W43", use_llm=False, today=date(2026, 10, 28)))
        s2 = asyncio.run(ps.run_period(repo, "stage", "week", "2026-W43", use_llm=False, today=date(2026, 10, 28)))
        self.assertEqual(s1["pipeline"]["status"], "partial")
        self.assertTrue(s2.get("unchanged"))
        self.assertEqual(repo.saved, 1)


class ComplaintsTest(unittest.TestCase):
    """Жалобы: заявитель из XML, сводка документов одной жалобы, метрики и риски организаций."""

    def test_facts(self):
        """Заявитель и итог из XML жалобы и решения."""
        f = complaint_facts("complaint", {"complaint": {"commonInfo": {"complaintNumber": "202600001"},
                                                        "applicantNew": {"legalEntity": {"INN": "7701234567",
                                                                                         "fullName": "ООО «А»"}}}})
        self.assertEqual((f["complainant_inn"], f["complaint_number"]), ("7701234567", "202600001"))
        d = complaint_facts("complaintDecision", {"checkResult": {"complResult": "COMPLAINT_PARTLY_VALID"}})
        self.assertEqual(d["complaint_result"], "partly")
        self.assertEqual(complaint_facts("complaintWithdraw", {})["complaint_result"], "withdrawn")

    def rows(self):
        """Две жалобы поставщика 77… на заказчика 24…: одна с решением «обоснована», вторая — необоснована."""
        base = {"complainant_inn": "7701234567", "customer_inn": "2457007351"}
        return [{"id": 1, "complaint_number": "1", "purchase_number": "P1", "tag": "complaint", **base},
                {"id": 2, "complaint_number": "1", "purchase_number": "P1", "tag": "complaintDecision",
                 "complaint_result": "justified", **base},
                {"id": 3, "complaint_number": "2", "purchase_number": "P2", "complaint_result": "unfounded", **base},
                {"id": 4, "complaint_number": "3", "purchase_number": "P3", "complaint_result": "justified",
                 "complainant_inn": "5000000000", "customer_inn": "2457007351"}]

    def test_customer(self):
        """Жалобы на заказчика: документы одной жалобы — одна запись; обоснованные → BEH-001."""
        items = org_profile.complaints(self.rows(), "2457007351", "customer")
        self.assertEqual(len(items), 3)
        m = org_profile.complaint_metrics(items, [{"number": "P1"}, {"number": "P9"}], "customer")
        self.assertEqual((m["justified"], m["unfounded"], m["purchases"]), (2, 1, 3))
        risks = org_profile._complaint_risks(m, "customer", settings)
        self.assertEqual([r["rule_id"] for r in risks], ["BEH-001"])

    def test_supplier(self):
        """Жалобы поставщика: победа после жалобы; мало жалоб — без риска."""
        items = org_profile.complaints(self.rows(), "7701234567", "supplier")
        m = org_profile.complaint_metrics(items, [{"number": "P2"}], "supplier")
        self.assertEqual((m["total"], m["customers"], m["won_after_complaint"]), (2, 1, 1))
        self.assertEqual(org_profile._complaint_risks(m, "supplier", settings), [])
        many = [{"key": str(i), "purchase_number": f"P{i}", "customer_inn": "1", "result": "unfounded"} for i in range(6)]
        mm = org_profile.complaint_metrics(many, [], "supplier")
        self.assertEqual([r["rule_id"] for r in org_profile._complaint_risks(mm, "supplier", settings)], ["BEH-002"])


if __name__ == "__main__":
    unittest.main()

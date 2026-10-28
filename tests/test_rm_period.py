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

    def facts(self, name):
        """Сведения XML ЕИС из ``tests/data/rm`` (реальные документы 05.2026, без подписи и контактов)."""
        from pathlib import Path

        from risk_monitoring import xml_events as xe

        tag, _, body = xe.parse_xml((Path(__file__).parent / "data" / "rm" / name).read_bytes())
        return xe.extract_facts(tag, body)

    def test_real_xml(self):
        """Жалоба: заявитель, номер, на оператора площадки, заказчик из ИКЗ; отзыв — только наименование и
        номер; приостановка — номер жалобы и закупки."""
        f = self.facts("complaint.xml")
        self.assertEqual((f["complainant_inn"], f["complaint_number"], f["complaint_target"]),
                         ("7801731943", "202600100161005752", "EP"))
        self.assertEqual(f["customer_inn"], "7707290995")
        self.assertIn("ЭТП", f["complaint_subject"])
        c = self.facts("complaintCancel.xml")
        self.assertEqual((c["complaint_number"], c["complaint_result"]), ("202600132489008095", "withdrawn"))
        self.assertIn("ВЫСОТА", c["complainant_name"])
        self.assertNotIn("complainant_inn", c)
        t = self.facts("tenderSuspension.xml")
        self.assertEqual((t["complaint_number"], t["purchase_number"]), ("202600100161003481", "0373200563926000023"))

    def test_real_decision_223(self):
        """Решение УФАС по жалобе 223-ФЗ: номер «дата.номер», CONFIRMED + нарушения выявлены → обоснована."""
        f = self.facts("complaintDecision223.xml")
        self.assertEqual((f["complaint_number"], f["complaint_result"], f["complaint_violation"]),
                         ("19.10.2026.307017", "justified", True))
        self.assertNotIn("complainant_inn", f)

    def test_result_codes(self):
        """«Не подтверждена» не путается с «подтверждена»; без решения — по признаку нарушения."""
        self.assertEqual(complaint_facts("complaintDecision", {"complaintDecisionSolution": "NOT_CONFIRMED"})
                         ["complaint_result"], "unfounded")
        self.assertEqual(complaint_facts("complaintDecision", {"complaintDecisionSolution": "PARTIALLY_CONFIRMED"})
                         ["complaint_result"], "partly")
        self.assertEqual(complaint_facts("complaintDecision", {"complaintDecisionViolation": "N"})
                         ["complaint_result"], "unfounded")

    def test_real_decision_44(self):
        """Решение по жалобе 44-ФЗ (checkResult с блоком complaint): номер жалобы — из complaint, а не номер
        решения; итог, заказчик, заявитель из имени файла; событие — жалоба, уровень риска по итогу."""
        from pathlib import Path

        from risk_monitoring import xml_events as xe

        f = self.facts("checkResult_complaint_unfounded.xml")
        self.assertEqual((f["complaint_number"], f["complaint_result"], f["complaint_target"]),
                         ("202600132489019607", "unfounded", "customer"))
        self.assertEqual((f["customer_inn"], f["purchase_number"]), ("7725060786", "0373200026426000249"))
        self.assertEqual(f["complainant_name"], "ООО «Рэлкон Строй»")
        j = self.facts("checkResult_complaint_justified.xml")
        self.assertEqual(j["complaint_result"], "justified")
        tag, ver, body = xe.parse_xml((Path(__file__).parent / "data" / "rm" / "checkResult_complaint_justified.xml").read_bytes())
        ev = xe.classify(tag, ver, False, [], j)
        self.assertEqual(ev["code"], "CMP-001")
        self.assertEqual([(r["rule_id"], r["level"]) for r in xe.rule_risks(ev, [], j)], [("PROC-008", 0.8)])
        self.assertEqual(xe.rule_risks(ev, [], f)[0]["level"], 0.2)

    def test_unplanned_check_not_complaint(self):
        """checkResult без блока complaint — результат внеплановой проверки, не жалоба."""
        from pathlib import Path

        from risk_monitoring import xml_events as xe

        f = self.facts("checkResult_unplanned.xml")
        self.assertNotIn("complaint_number", f)
        tag, ver, _ = xe.parse_xml((Path(__file__).parent / "data" / "rm" / "checkResult_unplanned.xml").read_bytes())
        self.assertEqual(xe.classify(tag, ver, False, [], f)["code"], "CTL-001")

    def test_decision_code(self):
        """Итог по коду решения."""
        d = complaint_facts("complaintDecision", {"checkResult": {"complResult": "COMPLAINT_PARTLY_VALID"}})
        self.assertEqual(d["complaint_result"], "partly")

    def rows(self):
        """Жалобы поставщика 77… на закупки заказчика 24…: №1 — обоснована (решение без ИНН), №2 — отозвана
        (отзыв без ИНН), №3 — на оператора площадки, обоснована; №4 — другого заявителя по номеру закупки."""
        base = {"complainant_inn": "7701234567", "customer_inn": "2457007351"}
        return [{"id": 1, "complaint_number": "1", "purchase_number": "P1", "tag": "complaint", **base},
                {"id": 2, "complaint_number": "1", "tag": "complaintDecision", "complaint_result": "justified"},
                {"id": 3, "complaint_number": "2", "purchase_number": "P2", **base},
                {"id": 4, "complaint_number": "2", "tag": "complaintCancel", "complaint_result": "withdrawn"},
                {"id": 5, "complaint_number": "3", "purchase_number": "P3", "complaint_target": "EP",
                 "complaint_result": "justified", **base},
                {"id": 6, "complaint_number": "4", "purchase_number": "P4", "complaint_result": "justified",
                 "complainant_inn": "5000000000"}]

    def test_customer(self):
        """Документы одной жалобы сводятся по номеру; жалоба на оператора не считается обоснованной против
        заказчика; закупка заказчика находится и без ИНН (по номеру)."""
        items = org_profile.complaints(self.rows(), "2457007351", "customer", ["P4"])
        self.assertEqual([c["key"] for c in items], ["1", "2", "3", "4"])
        m = org_profile.complaint_metrics(items, [{"number": "P1"}], "customer")
        self.assertEqual((m["justified"], m["withdrawn"], m["against_operator"]), (2, 1, 1))
        self.assertEqual([r["rule_id"] for r in org_profile._complaint_risks(m, "customer", settings)], ["BEH-001"])

    def test_supplier(self):
        """Жалобы поставщика: победа после жалобы; мало жалоб — без риска; много необоснованных — BEH-002."""
        items = org_profile.complaints(self.rows(), "7701234567", "supplier")
        self.assertEqual(len(items), 3)
        m = org_profile.complaint_metrics(items, [{"number": "P2"}], "supplier")
        self.assertEqual((m["customers"], m["won_after_complaint"]), (1, 1))
        self.assertEqual(org_profile._complaint_risks(m, "supplier", settings), [])
        many = [{"key": str(i), "purchase_number": f"P{i}", "customer_inn": "1", "result": "unfounded"} for i in range(6)]
        mm = org_profile.complaint_metrics(many, [], "supplier")
        self.assertEqual([r["rule_id"] for r in org_profile._complaint_risks(mm, "supplier", settings)], ["BEH-002"])


if __name__ == "__main__":
    unittest.main()

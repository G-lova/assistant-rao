"""Внеплановые проверки контрольных органов (``risk_monitoring.control_checks``)."""
import unittest
from pathlib import Path

from configs import settings
from risk_monitoring import control_checks as cc
from risk_monitoring import xml_events as xe

DATA = Path(__file__).parent / "data" / "rm"


def _facts(name):
    """Сведения XML из ``tests/data/rm``."""
    tag, _, body = xe.parse_xml((DATA / name).read_bytes())
    return xe.extract_facts(tag, body)


class CheckFactsTest(unittest.TestCase):
    """Разбор результата проверки ФАС России от 27.10.2026 (реальный XML без подписей)."""

    def test_facts(self):
        """Номер, нарушения, предписание, субъект — специализированная организация, все закупки и заказчики."""
        f = _facts("checkResult_unplanned.xml")
        self.assertEqual((f["check_number"], f["check_result"], f["check_prescription"]),
                         ("202600100161017921", "violations", True))
        self.assertEqual(f["check_subjects"][0]["role"], "specialized")
        self.assertEqual(len(f["check_purchases"]), 50)
        self.assertIn("0711200012826000689", f["check_purchases"])
        self.assertGreater(len(f["check_customer_inns"]), 30)

    def test_joint_purchase_codes(self):
        """Совместная закупка: несколько ``purchaseCode`` (список) — ИНН всех заказчиков, а не только первого."""
        xml = (b'<ns3:export xmlns:ns3="http://zakupki.gov.ru/oos/export/1"><ns3:checkResult>'
               b'<commonInfo><regNumber>202600100161099999</regNumber><versionNumber>1</versionNumber></commonInfo>'
               b'<base><unplannedCheck><regNumber>202600100161088888</regNumber>'
               b'<orders><purchase><purchaseNumber>0111200000126000001</purchaseNumber><purchaseCodes>'
               b'<purchaseCode>262111111111177100100100010010000244</purchaseCode>'
               b'<purchaseCode>262222222222277100100100010010000244</purchaseCode>'
               b'</purchaseCodes></purchase></orders>'
               b'<checkResult>VIOLATIONS</checkResult></unplannedCheck></base></ns3:checkResult></ns3:export>')
        tag, _, body = xe.parse_xml(xml)
        f = xe.extract_facts(tag, body)
        self.assertEqual(set(f["check_customer_inns"]), {"1111111111", "2222222222"})

    def test_cyrillic_number_and_act_date(self):
        """Номер с кириллической «А» распознаётся; дата берётся из акта при отсутствии решения."""
        f = _facts("checkResult_act_cyr_number.xml")
        self.assertEqual(f["check_number"], "2026452А3086001084")
        self.assertEqual(f["check_result"], "no_violations")
        self.assertEqual(f["check_decision_date"], "2026-10-30")


class ReparseTest(unittest.TestCase):
    """Пересборка ветки ``facts`` в сохранённом ``ai_analysis`` (``risk_monitoring.reparse_checks``)."""

    XML = (b'<ns3:export xmlns:ns3="http://zakupki.gov.ru/oos/export/1"><ns3:checkResult>'
           b'<commonInfo><regNumber>202600100161099999</regNumber><versionNumber>1</versionNumber></commonInfo>'
           b'<base><unplannedCheck><regNumber>202600100161088888</regNumber>'
           b'<orders><purchase><purchaseNumber>0111200000126000001</purchaseNumber><purchaseCodes>'
           b'<purchaseCode>262111111111177100100100010010000244</purchaseCode>'
           b'<purchaseCode>262222222222277100100100010010000244</purchaseCode>'
           b'</purchaseCodes></purchase></orders>'
           b'<checkResult>VIOLATIONS</checkResult></unplannedCheck></base></ns3:checkResult></ns3:export>')

    def test_rebuilt_replaces_facts_and_keeps_rest(self):
        """Меняется только ``facts``; остальные ветки сохранены; повторный вызов возвращает ``None``."""
        from risk_monitoring import reparse_checks as rp

        tag, _, body = xe.parse_xml(self.XML)
        current = {"pipeline": {"status": "done"}, "facts": {"check_number": "202600100161088888",
                                                             "check_customer_inns": ["1111111111"]}}
        new = rp.rebuilt_ai_analysis(current, tag, body)
        self.assertIsNotNone(new)
        self.assertEqual(new["pipeline"], {"status": "done"})  # не тронуто
        self.assertEqual(set(new["facts"]["check_customer_inns"]), {"1111111111", "2222222222"})
        self.assertIsNone(rp.rebuilt_ai_analysis(new, tag, body))  # идемпотентность

    def test_rebuilt_accepts_json_string(self):
        """``ai_analysis`` строкой JSON тоже принимается."""
        import json

        from risk_monitoring import reparse_checks as rp

        tag, _, body = xe.parse_xml(self.XML)
        new = rp.rebuilt_ai_analysis(json.dumps({"facts": {}}), tag, body)
        self.assertEqual(len(new["facts"]["check_customer_inns"]), 2)


class CheckDedupTest(unittest.TestCase):
    """Детерминированный выбор актуальной редакции при повторе номера проверки."""

    def test_latest_version_wins(self):
        """При двух строках с одним номером побеждает большая редакция независимо от порядка строк."""
        v1 = {"id": 1, "check_number": "N1", "check_version": 1, "check_result": "no_violations",
              "check_purchases": '["P1"]', "check_customer_inns": '["111"]'}
        v2 = {"id": 2, "check_number": "N1", "check_version": 2, "check_result": "violations",
              "check_purchases": '["P1", "P2"]', "check_customer_inns": '["111"]'}
        for rows in ([v1, v2], [v2, v1]):
            out = cc.normalize(rows)
            self.assertEqual(len(out), 1)
            self.assertEqual((out[0]["result"], out[0]["xml_id"]), ("violations", 2))


class CheckRisksTest(unittest.TestCase):
    """Риски закупки и заказчика."""

    def setUp(self):
        """Проверка из реального XML и проверка самого заказчика."""
        self.real = cc.normalize([{"id": 9, **_facts("checkResult_unplanned.xml")}])
        self.own = cc.normalize([{"id": 10, "check_number": "202600100161000001", "check_result": "violations",
                                  "check_prescription": "false", "check_subjects": '[{"role": "customer", "inn": "111"}]',
                                  "check_purchases": '["P1", "P2"]', "check_customer_inns": '["111"]'}])

    def test_other_subject_weak(self):
        """Проверяли организацию, проводившую закупку, — слабый риск, закупка одна из 50."""
        r = cc.check_risks(self.real, "0711200012826000689", "1655018240", settings)
        self.assertEqual(r[0]["rule_id"], "PROC-008")
        self.assertEqual(r[0]["level"], round(settings.CHECK_RISK_LEVELS["other"] + settings.CHECK_PRESCRIPTION_BONUS, 2))
        self.assertIn("одна из 50", r[0]["description"])
        self.assertEqual(cc.check_risks(self.real, "0000000000000000000", "1", settings), [])

    def test_own_subject_strong(self):
        """Проверяли самого заказчика — сильный риск; у заказчика — BEH-001."""
        r = cc.check_risks(self.own, "P1", "111", settings)
        self.assertEqual(r[0]["level"], settings.CHECK_RISK_LEVELS["customer"])
        m = cc.customer_metrics(self.own + self.real, "111", ["P1"])
        self.assertEqual((m["as_subject"], m["as_subject_violations"], m["via_purchases"]), (1, 1, 0))
        self.assertEqual([x["rule_id"] for x in cc.customer_risks(m, settings)], ["BEH-001"])

    def test_customer_via_purchases_no_risk(self):
        """Проверка организации, проводившей закупки заказчика, — метрика без риска."""
        m = cc.customer_metrics(self.real, "1655018240", ["0711200012826000689"])
        self.assertEqual((m["as_subject"], m["via_purchases"], m["purchases_checked"]), (0, 1, 1))
        self.assertEqual(cc.customer_risks(m, settings), [])
        self.assertIsNone(cc.customer_metrics(self.real, "0000000000", []))

    def test_in_contract_analysis(self):
        """Закупка из списка проверки получает PROC-008 и блок control_checks."""
        from tests.test_rm_package import CARD
        from risk_monitoring import contract_package as cp

        chk = cc.normalize([{"id": 11, "check_number": "202600100161000002", "check_result": "violations",
                             "check_purchases": ["0354100008624000011"], "check_subjects": []}])
        b = cp.build_contract_analysis(card=CARD, files=[], xmls=[], values=None, black=[], statuses={}, checks=chk)
        self.assertIn("PROC-008", [r["rule_id"] for r in b["risks"]])
        self.assertEqual(b["control_checks"][0]["number"], "202600100161000002")


if __name__ == "__main__":
    unittest.main()

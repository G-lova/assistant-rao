"""Стадия закупки и наложенные состояния (purchase_status)."""
import unittest

from risk_monitoring import purchase_status as ps


def xml(xid, code, tag, at, action=None, version=0):
    """XML закупки с событием."""
    ev = {"code": code, "event_at": at}
    if action:
        ev["suspension_action"] = action
    return {"id": xid, "xml_tag": tag, "eis_version": version, "event": ev}


CONTRACT = {"contract_details": {"num_contr": "1", "contr_status": "Исполнение"}}


class StageTest(unittest.TestCase):
    """Стадия: статус ЕИС для конечных состояний, факты и события — для остальных."""

    def test_completed_only_by_status(self):
        """final_stage и d_contr_fact этапа не делают контракт исполненным; статус «Исполнение завершено» — делает."""
        d = {**CONTRACT, "procedure": {"final_stage": "true", "stage_start": "2026-01-01"},
             "ispolnenie": {"d_contr_fact": "2026-03-01"}}
        self.assertEqual(ps.stage(d)["stage"], "execution")
        d["contract_details"] = {"num_contr": "1", "contr_status": "Исполнение завершено"}
        r = ps.stage(d)
        self.assertEqual((r["stage"], r["name"]), ("completed", "исполнен"))
        self.assertIn("Исполнение завершено", r["basis"])

    def test_terminated_annulled(self):
        """Прекращено / аннулировано по статусу; расторжение по карточке и по событию CON-007."""
        self.assertEqual(ps.stage({"contract_details": {"num_contr": "1", "contr_status": "Исполнение прекращено"}})["stage"],
                         "terminated")
        self.assertEqual(ps.stage({"contract_details": {"num_contr": "1", "contr_status": "Аннулировано"}})["stage"],
                         "annulled")
        self.assertEqual(ps.stage({**CONTRACT, "procedure": {"term_date": "2026-05-01"}})["stage"], "terminated")
        self.assertEqual(ps.stage(CONTRACT, [xml(1, "CON-007", "contractCancellation", "2026-05-01")])["stage"],
                         "terminated")
        self.assertEqual(ps.stage(CONTRACT)["stage"], "contract")

    def test_cancelled_and_resumed(self):
        """Отмена без контракта — cancelled; возобновление после отмены — снова notice."""
        cancel = xml(2, "PUR-008", "epNotificationCancel", "2026-04-02")
        self.assertEqual(ps.stage({}, [xml(1, "PUR-001", "epNotificationEF2020", "2026-04-01"), cancel])["stage"],
                         "cancelled")
        resumed = xml(3, "PUR-009", "epNotificationCancelFailure", "2026-04-03")
        self.assertEqual(ps.stage({}, [cancel, resumed])["stage"], "notice")
        self.assertEqual(ps.stage({}, [xml(4, "PUR-016", "epProtocolCancel", "2026-04-05")])["stage"], "cancelled")

    def test_failed(self):
        """Нет заявок — failed; потом контракт с единственным участником или итоги — нет."""
        absent = xml(1, "PUR-012", "epNoticeApplicationsAbsence", "2026-04-10")
        self.assertEqual(ps.stage({}, [absent])["stage"], "failed")
        # отзыв одной заявки — не неудача закупки
        self.assertEqual(ps.stage({}, [xml(2, "PUR-012", "epNoticeApplicationCancel", "2026-04-10")])["stage"], "notice")
        self.assertEqual(ps.stage({}, [absent, xml(3, "PUR-010", "epProtocolEF2020Final", "2026-04-12")])["stage"],
                         "award")

    def test_contract_beats_events(self):
        """Контракт в реестре важнее старой отмены (закупку возобновили и заключили контракт)."""
        self.assertEqual(ps.stage(CONTRACT, [xml(1, "PUR-008", "epNotificationCancel", "2026-04-02")])["stage"],
                         "contract")


class AwardTest(unittest.TestCase):
    """Заключение контракта и аннулирование."""

    def test_award(self):
        """Итоги, проект, подписание, уклонение — award; отмена процедуры заключения — cancelled, её отмена — снова award."""
        self.assertEqual(ps.stage({}, [xml(1, "PUR-010", "epProtocolEF2020Final", "2026-04-12")])["stage"], "award")
        self.assertEqual(ps.stage({}, [xml(1, "PUR-015", "cpContractProject", "2026-04-13"),
                                      xml(2, "CON-012", "cpNoticeEvasion", "2026-04-20")])["stage"], "award")
        cancel = xml(3, "CON-012", "cpProcedureCancel", "2026-04-21")
        self.assertEqual(ps.stage({}, [xml(1, "PUR-015", "cpContractProject", "2026-04-13"), cancel])["stage"],
                         "cancelled")
        # отмена отмены — снова заключение контракта
        self.assertEqual(ps.stage({}, [xml(1, "PUR-015", "cpContractProject", "2026-04-13"), cancel,
                                      xml(4, "CON-008", "cpProcedureCancelFailure", "2026-04-22")])["stage"], "award")
        self.assertEqual(ps.STAGE_NAMES["award"], "заключение контракта")

    def test_annulled_by_document(self):
        """Документ «Аннулирование сведений о контракте» без статуса — annulled."""
        self.assertEqual(ps.stage(CONTRACT, [xml(1, "CON-008", "contractCancel", "2026-06-01")])["stage"], "annulled")


class FlagsTest(unittest.TestCase):
    """Приостановка закупки по жалобе."""

    def test_suspended_and_reopened(self):
        """Приостановка открыта, пока нет возобновления, решения по жалобе или хода процедуры."""
        susp = xml(1, "CMP-001", "tenderSuspension", "2026-05-08", action="SUSPEND")
        self.assertEqual(ps.flags([susp])["suspended"]["since"], "2026-05-08")
        reopen = xml(2, "CMP-001", "tenderSuspension", "2026-05-12", action="REOPENING")
        self.assertEqual(ps.flags([susp, reopen]), {})
        decision = xml(3, "CMP-001", "checkResult", "2026-05-11")
        self.assertEqual(ps.flags([susp, decision]), {})
        self.assertEqual(ps.flags([susp, xml(4, "PUR-010", "epProtocolEF2020Final", "2026-05-20")]), {})
        self.assertEqual(ps.flags([susp, xml(5, "PUR-007", "epClarificationDoc", "2026-05-09")])["suspended"]["xml_id"], 1)

    def test_old_suspension_without_action(self):
        """Без action (XML до 11.11.2026): открыта, только если после неё ничего не было."""
        old = xml(1, "CMP-001", "tenderSuspension", "2026-05-08")
        self.assertIn("suspended", ps.flags([old]))
        self.assertEqual(ps.flags([old, xml(2, "PUR-007", "epClarificationDoc", "2026-05-09")]), {})

    def test_json_strings(self):
        """event строкой JSON (из внешней БД) разбирается."""
        x = {"id": 1, "xml_tag": "tenderSuspension", "event": '{"code": "CMP-001", "event_at": "2026-05-08", '
                                                             '"suspension_action": "SUSPEND"}'}
        self.assertIn("suspended", ps.flags([x]))


class MoreFlagsTest(unittest.TestCase):
    """Жалобы, проверки, предписание, односторонний отказ, претензии, РНП."""

    def test_complaints(self):
        """Жалоба открыта до решения с тем же номером; решение по другой жалобе её не закрывает."""
        def c(xid, tag, at, num=None):
            """XML жалобы с номером."""
            x = xml(xid, "CMP-001", tag, at)
            if num:
                x["event"]["complaint_number"] = num
            return x
        f = ps.flags([c(1, "complaint", "2026-05-01", "A"), c(2, "complaint", "2026-05-02", "B"),
                      c(3, "checkResult", "2026-05-10", "A")])
        self.assertEqual((f["complaint_open"]["count"], f["complaint_open"]["numbers"]), (1, ["B"]))
        self.assertNotIn("complaint_open", ps.flags([c(1, "complaint", "2026-05-01"), c(2, "complaintCancel", "2026-05-03")]))
        # результат проверки (CTL-001), а не решение по жалобе, жалобу не закрывает
        f = ps.flags([c(1, "complaint", "2026-05-01"), xml(2, "CTL-001", "checkResult", "2026-05-03")])
        self.assertIn("complaint_open", f)
        # у конечной стадии признак не ставится
        self.assertEqual(ps.flags([c(1, "complaint", "2026-05-01")], stage="cancelled"), {})

    def test_inspection_and_prescription(self):
        """Проверка без результата — inspection_open; проверка с нарушениями и предписанием по закупке — prescription."""
        self.assertIn("inspection_open", ps.flags([xml(1, "CTL-001", "unplannedCheck", "2026-05-01")]))
        self.assertEqual(ps.flags([xml(1, "CTL-001", "unplannedCheck", "2026-05-01"),
                                   xml(2, "CTL-001", "checkResult", "2026-05-20")]), {})
        checks = [{"number": "7", "result": "violations", "prescription": True, "date": "2026-05-20",
                   "purchases": {"0123"}},
                  {"number": "8", "result": "no_violations", "prescription": False, "date": "2026-05-21",
                   "purchases": {"0123"}}]
        f = ps.flags([], stage="contract", checks=checks, purchase_number="0123")
        self.assertEqual((f["prescription"]["check_number"], f["prescription"]["count"]), ("7", 1))
        self.assertEqual(ps.flags([], stage="completed", checks=checks, purchase_number="0123"), {})

    def test_termination_pending(self):
        """Односторонний отказ — ждёт расторжения; отмена отказа или расторжение — признак снимается."""
        refusal = xml(1, "CON-009", "contractProcedureUnilateralRefusal", "2026-06-01")
        f = ps.flags([refusal], stage="execution")
        self.assertIn("termination_pending", f)
        self.assertEqual(f["rnp"]["status"], "expected")
        self.assertNotIn("termination_pending",
                         ps.flags([refusal, xml(2, "CON-008", "contractProcedureUnilateralRefusalCancel", "2026-06-05")]))
        self.assertNotIn("termination_pending",
                         ps.flags([refusal, xml(3, "CON-007", "contractCancellation", "2026-06-12")]))
        self.assertNotIn("termination_pending", ps.flags([refusal], stage="terminated"))

    def test_claims_and_rnp(self):
        """Претензии — число и дата; РНП: запись найдена — included, уклонение без записи — expected."""
        f = ps.flags([xml(1, "CON-009", "claimsCorrespondenceNotice", "2026-06-01"),
                      xml(2, "CON-009", "claimsCorrespondenceNotice", "2026-06-07")], stage="execution")
        self.assertEqual((f["claims"]["count"], f["claims"]["last"]), (2, "2026-06-07"))
        self.assertEqual(ps.flags([xml(1, "PUR-013", "epProtocolEvasion", "2026-04-20")])["rnp"]["status"], "expected")
        self.assertEqual(ps.flags([], rnp=[{"id": 5}])["rnp"]["status"], "included")


if __name__ == "__main__":
    unittest.main()

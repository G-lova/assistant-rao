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
                         "notice")

    def test_contract_beats_events(self):
        """Контракт в реестре важнее старой отмены (закупку возобновили и заключили контракт)."""
        self.assertEqual(ps.stage(CONTRACT, [xml(1, "PUR-008", "epNotificationCancel", "2026-04-02")])["stage"],
                         "contract")


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


if __name__ == "__main__":
    unittest.main()

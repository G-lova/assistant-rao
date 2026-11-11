"""Стадия закупки и наложенные состояния (решение 11.11.2026).

**Стадия** — одно значение, положение закупки в жизненном цикле (``passport.stage``, во внешней БД ``rm_stage``):

==============  ==========================================================================================
``notice``      определение поставщика: извещение есть, контракта нет
``cancelled``   закупка отменена без контракта: PUR-008 «Отменена закупка», PUR-016 «Отмена определения
                поставщика» — если после неё нет возобновления (PUR-009) и хода процедуры
``failed``      закупка не состоялась: нет заявок (``epNoticeApplicationsAbsence``) и после этого нет
                протокола итогов, проекта контракта или контракта
``contract``    контракт заключён (номер в реестре), фактов исполнения нет
``execution``   есть факты исполнения: начат этап, оплата, документ приёмки
``completed``   **только по статусу ЕИС** «Исполнение завершено». ``procedure.final_stage`` и
                ``ispolnenie.d_contr_fact`` относятся к последнему этапу, а не ко всему контракту: по ним 27 из
                28 «исполненных» в выгрузке были в статусе «Исполнение»
``terminated``  расторгнут: статус «Исполнение прекращено», данные о расторжении в ``procedure`` или
                событие CON-007 «Расторжение контракта»
``annulled``    статус ЕИС «Аннулировано» — запись реестра контрактов аннулирована
==============  ==========================================================================================

Статус контракта ЕИС (``contract_details.contr_status``) — источник истины для конечных состояний; правила по
фактам и событиям — там, где статуса нет (до контракта) или он один на несколько стадий («Исполнение» —
и ``contract``, и ``execution``).

**Наложенные состояния** (``passport.flags``) — не меняют стадию, у каждого есть начало и конец:

* ``suspended`` — закупка приостановлена контрольным органом по жалобе (``tenderSuspension`` с ``action``
  SUSPEND…). Снимается возобновлением (``tenderSuspension`` REOPENING), решением по жалобе, отзывом жалобы или
  дальнейшим ходом процедуры (протоколы, контракт). У XML, разобранных до 11.11.2026, ``action`` в событии нет —
  такая приостановка считается открытой, только если после неё по закупке ничего не происходило.

События берутся из всех версий XML закупки по дате события: обратимые переходы (возобновление после отмены,
снятие приостановки) учитываются.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

STAGE_NAMES = {
    "notice": "определение поставщика", "cancelled": "закупка отменена", "failed": "закупка не состоялась",
    "contract": "контракт заключён", "execution": "исполнение", "completed": "исполнен", "terminated": "расторгнут",
    "annulled": "контракт аннулирован",
}
FINAL_STAGES = {"cancelled", "failed", "completed", "terminated", "annulled"}

STATUS_STAGE = {"исполнение завершено": "completed", "исполнение прекращено": "terminated", "аннулировано": "annulled"}

CANCEL_CODES = {"PUR-008", "PUR-016"}
RESUME_CODES = {"PUR-009"}
# ход процедуры после отмены / неудачи / приостановки: заявки рассмотрены, итоги, проект контракта, контракт
PROGRESS_CODES = {"PUR-010", "PUR-011", "PUR-015", "CON-001", "CON-011"}
NO_APPLICATIONS_TAGS = {"epNoticeApplicationsAbsence"}
# документы, закрывающие жалобу: решение (44-ФЗ — checkResult с блоком complaint, 223-ФЗ — complaintDecision),
# отзыв, возврат
COMPLAINT_CLOSE_TAGS = {"checkResult", "complaintDecision", "complaintCancel", "complaintWithdraw",
                        "closedComplaintCancel", "parElectronicComplaintRefusal", "closedParElectronicComplaintRefusal"}


def _g(d: Dict[str, Any], *path: str) -> Any:
    """Значение по пути в ``dataset``."""
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _flag(v: Any) -> bool:
    """Логическое значение из ``dataset`` (``true`` / ``1`` / ``"true"``)."""
    return str(v).strip().lower() in ("true", "1", "да", "yes") if v is not None else False


def _as_dict(v: Any) -> Dict[str, Any]:
    """JSON-поле строки (строка или словарь) → словарь."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        import json

        try:
            return json.loads(v)
        except ValueError:
            return {}
    return {}


def timeline(xmls: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """События закупки по возрастанию даты: ``code``, ``tag``, ``at``, ``action`` (приостановка), ``xml_id``."""
    out = []
    for x in xmls or []:
        ev = _as_dict(x.get("event"))
        code = ev.get("code")
        tag = x.get("xml_tag") or ev.get("xml_tag")
        if not code and not tag:
            continue
        out.append({"code": code, "tag": tag, "at": str(ev.get("event_at") or x.get("exact_date") or "")[:19],
                    "action": str(ev.get("suspension_action") or "").upper() or None,
                    "complaint_result": ev.get("complaint_result"), "xml_id": x.get("id"),
                    "version": x.get("eis_version")})
    out.sort(key=lambda e: (e["at"], int(e.get("version") or 0), int(e.get("xml_id") or 0)))
    return out


def _last_index(events: List[Dict[str, Any]], pred) -> int:
    """Индекс последнего события, удовлетворяющего условию (−1 — нет)."""
    for i in range(len(events) - 1, -1, -1):
        if pred(events[i]):
            return i
    return -1


def pre_contract_stage(events: List[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Стадия до заключения контракта по событиям: ``notice`` / ``cancelled`` / ``failed`` и событие-основание."""
    i_cancel = _last_index(events, lambda e: e["code"] in CANCEL_CODES)
    i_fail = _last_index(events, lambda e: e["tag"] in NO_APPLICATIONS_TAGS)
    i_back = _last_index(events, lambda e: e["code"] in RESUME_CODES or e["code"] in PROGRESS_CODES)
    if i_cancel >= 0 and i_cancel > i_back:
        return "cancelled", events[i_cancel]
    i_progress = _last_index(events, lambda e: e["code"] in PROGRESS_CODES - {"PUR-011"})
    if i_fail >= 0 and i_fail > i_progress:
        return "failed", events[i_fail]
    return "notice", None


def stage(dataset: Dict[str, Any], xmls: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Стадия закупки.

    Args:
        dataset: ``dataset`` карточки.
        xmls: XML закупки (``event``, ``xml_tag``, ``exact_date``) — для отмены, неудачи и расторжения.

    Returns:
        Dict[str, Any]: ``stage``, ``name``, ``basis`` (почему: статус ЕИС, данные карточки, событие),
        ``contract_status``.
    """
    status = str(_g(dataset, "contract_details", "contr_status") or "").strip()
    events = timeline(xmls)

    def out(code: str, basis: str) -> Dict[str, Any]:
        """Результат с названием стадии."""
        return {"stage": code, "name": STAGE_NAMES[code], "basis": basis, "contract_status": status or None}

    by_status = STATUS_STAGE.get(status.lower())
    if by_status:
        return out(by_status, f"статус контракта в ЕИС «{status}»")
    if _g(dataset, "procedure", "term_code") or _g(dataset, "procedure", "term_date"):
        return out("terminated", "сведения о расторжении в карточке")
    if _g(dataset, "contract_details", "num_contr"):
        if any(e["code"] == "CON-007" for e in events):
            return out("terminated", "событие «Расторжение контракта»")
        if (_g(dataset, "procedure", "stage_start") or _g(dataset, "procedure", "paid_sum")
                or _g(dataset, "procedure", "acc_doc_num")):
            return out("execution", "есть сведения об исполнении (этап, оплата или приёмка)")
        return out("contract", "контракт в реестре, сведений об исполнении нет")
    code, ev = pre_contract_stage(events)
    if ev:
        return out(code, f"событие «{ev['code'] or ev['tag']}» от {ev['at'][:10] or 'даты нет'}")
    return out(code, "извещение без контракта")


def flags(xmls: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Наложенные состояния закупки: сейчас — ``suspended`` (приостановка по жалобе).

    Returns:
        Dict[str, Any]: ``suspended`` — ``{"since": дата, "xml_id": …}``, если приостановка не снята; иначе ключа нет.
    """
    events = timeline(xmls)
    out: Dict[str, Any] = {}
    i_susp = _last_index(events, lambda e: e["tag"] == "tenderSuspension" and not (e["action"] or "").startswith("REOPEN"))
    if i_susp < 0:
        return out
    s = events[i_susp]
    later = events[i_susp + 1:]
    closed = any((e["tag"] == "tenderSuspension" and (e["action"] or "").startswith("REOPEN"))
                 or e["tag"] in COMPLAINT_CLOSE_TAGS or e["code"] in PROGRESS_CODES or e["code"] in CANCEL_CODES
                 for e in later)
    # XML без action (разобраны до 11.11.2026): открытой считаем, только если после неё ничего не было
    if not closed and (s["action"] or later == []):
        out["suspended"] = {"since": s["at"][:10] or None, "xml_id": s["xml_id"],
                            "action": s["action"], "title": "Закупка приостановлена по жалобе"}
    return out

"""Стадия закупки и наложенные состояния (решение 11.11.2026).

**Стадия** — одно значение, положение закупки в жизненном цикле (``passport.stage``, во внешней БД ``rm_stage``):

==============  ==========================================================================================
``notice``      определение поставщика: извещение есть, контракта нет
``cancelled``   закупка отменена без контракта: PUR-008 «Отменена закупка», PUR-016 «Отмена определения
                поставщика», отмена процедуры заключения контракта (``cpProcedureCancel``) — если после неё нет
                возобновления (PUR-009, ``cpProcedureCancelFailure``) и хода процедуры
``failed``      закупка не состоялась: нет заявок (``epNoticeApplicationsAbsence``) и после этого нет
                протокола итогов, проекта контракта или контракта
``award``       заключение контракта: подведены итоги (PUR-010), размещён проект контракта (PUR-015),
                идёт подписание (CON-011) или уклонение победителя (PUR-013, CON-012 — контракт со
                следующим участником); номера контракта в реестре ещё нет
``contract``    контракт заключён (номер в реестре), фактов исполнения нет
``execution``   есть факты исполнения: начат этап, оплата, документ приёмки
``completed``   **только по статусу ЕИС** «Исполнение завершено». ``procedure.final_stage`` и
                ``ispolnenie.d_contr_fact`` относятся к последнему этапу, а не ко всему контракту: по ним 27 из
                28 «исполненных» в выгрузке были в статусе «Исполнение»
``terminated``  расторгнут: статус «Исполнение прекращено», данные о расторжении в ``procedure`` или
                событие CON-007 «Расторжение контракта»
``annulled``    статус ЕИС «Аннулировано» или документ ``contractCancel`` — запись реестра контрактов
                аннулирована
==============  ==========================================================================================

Статус контракта ЕИС (``contract_details.contr_status``) — источник истины для конечных состояний; правила по
фактам и событиям — там, где статуса нет (до контракта) или он один на несколько стадий («Исполнение» —
и ``contract``, и ``execution``).

**Наложенные состояния** (``passport.flags``) — не меняют стадию, у каждого есть начало и конец (:func:`flags`):

* ``suspended`` — закупка приостановлена контрольным органом по жалобе (``tenderSuspension`` с ``action``
  SUSPEND…). Снимается возобновлением (``tenderSuspension`` REOPENING), решением по жалобе, отзывом жалобы или
  дальнейшим ходом процедуры (протоколы, контракт). У XML, разобранных до 11.11.2026, ``action`` в событии нет —
  такая приостановка считается открытой, только если после неё по закупке ничего не происходило;
* ``complaint_open`` — жалобы без решения, отзыва или возврата (по номеру жалобы; без номера — по порядку
  документов);
* ``inspection_open`` — внеплановая проверка (``unplannedCheck``, ``unplannedEvent`` …) без результата
  (``checkResult``) и отмены;
* ``prescription`` — проверка с нарушениями и предписанием, в список закупок которой входит закупка
  (``control_checks``); сведений об исполнении предписания в ЕИС нет — признак держится, пока стадия не конечная;
* ``termination_pending`` — односторонний отказ от исполнения контракта размещён, но не отменён и расторжение
  ещё не отражено (решение вступает в силу через 10 дней после уведомления, ч. 13 ст. 95 44-ФЗ);
* ``claims`` — претензионная переписка по контракту (число и последняя дата); итога переписки в ЕИС нет —
  признак держится, пока стадия не конечная;
* ``rnp`` — реестр недобросовестных поставщиков после уклонения победителя или одностороннего отказа заказчика:
  ``included`` — поставщик найден в РНП (``rnp_entries``), ``expected`` — основание есть, записи в РНП (пока) нет.

События берутся из всех версий XML закупки по дате события: обратимые переходы (возобновление после отмены,
снятие приостановки) учитываются.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

STAGE_NAMES = {
    "notice": "определение поставщика", "award": "заключение контракта", "cancelled": "закупка отменена",
    "failed": "закупка не состоялась",
    "contract": "контракт заключён", "execution": "исполнение", "completed": "исполнен", "terminated": "расторгнут",
    "annulled": "контракт аннулирован",
}
FINAL_STAGES = {"cancelled", "failed", "completed", "terminated", "annulled"}

STATUS_STAGE = {"исполнение завершено": "completed", "исполнение прекращено": "terminated", "аннулировано": "annulled"}

CANCEL_CODES = {"PUR-008", "PUR-016"}
CANCEL_TAGS = {"cpProcedureCancel", "pprf615ContractProcedureCancel", "cpProcedureCancelLKP"}
RESUME_CODES = {"PUR-009"}
RESUME_TAGS = {"cpProcedureCancelFailure"}
# заключение контракта: итоги, проект контракта, подписание, уклонение (контракт со следующим участником)
AWARD_CODES = {"PUR-010", "PUR-015", "CON-011", "PUR-013", "CON-012"}
ANNUL_TAGS = {"contractCancel"}
COMPLAINT_FILE_TAGS = {"complaint", "closedComplaint", "parElectronicComplaintAccept", "closedParElectronicComplaintAccept",
                       "complaintTransfer", "closedComplaintTransfer"}
INSPECTION_START_TAGS = {"unplannedCheck", "closedUnplannedCheck", "unplannedEvent", "unplannedCheckTenderSusp",
                         "closedUnplannedCheckTenderSusp", "complaintVerificationPlan"}
INSPECTION_CLOSE_TAGS = {"checkResult", "unplannedCheckCancel", "closedUnplannedCheckCancel", "unplannedEventCancel",
                         "complaintVerificationResult"}
REFUSAL_TAGS = {"contractProcedureUnilateralRefusal", "parContractProcedureUnilateralRefusal"}
REFUSAL_CANCEL_TAGS = {"contractProcedureUnilateralRefusalCancel", "parContractProcedureUnilateralRefusalCancel"}
CLAIM_TAGS = {"claimsCorrespondenceNotice", "parClaimsCorrespondenceNotice"}
EVASION_CODES = {"PUR-013", "CON-012"}
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
                    "complaint_result": ev.get("complaint_result"), "complaint_number": ev.get("complaint_number"),
                    "xml_id": x.get("id"),
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
    """Стадия до заключения контракта по событиям: ``notice`` / ``award`` / ``cancelled`` / ``failed`` и событие-основание."""
    i_cancel = _last_index(events, lambda e: e["code"] in CANCEL_CODES or e["tag"] in CANCEL_TAGS)
    i_fail = _last_index(events, lambda e: e["tag"] in NO_APPLICATIONS_TAGS)
    i_back = _last_index(events, lambda e: e["code"] in RESUME_CODES or e["tag"] in RESUME_TAGS
                         or e["code"] in PROGRESS_CODES)
    if i_cancel >= 0 and i_cancel > i_back:
        return "cancelled", events[i_cancel]
    i_progress = _last_index(events, lambda e: e["code"] in PROGRESS_CODES - {"PUR-011"})
    if i_fail >= 0 and i_fail > i_progress:
        return "failed", events[i_fail]
    i_award = _last_index(events, lambda e: e["code"] in AWARD_CODES)
    if i_award >= 0:
        return "award", events[i_award]
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
        if any(e["tag"] in ANNUL_TAGS for e in events):
            return out("annulled", "документ «Аннулирование сведений о контракте»")
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


def _suspended(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Открытая приостановка закупки по жалобе."""
    i_susp = _last_index(events, lambda e: e["tag"] == "tenderSuspension" and not (e["action"] or "").startswith("REOPEN"))
    if i_susp < 0:
        return None
    s = events[i_susp]
    later = events[i_susp + 1:]
    closed = any((e["tag"] == "tenderSuspension" and (e["action"] or "").startswith("REOPEN"))
                 or e["tag"] in COMPLAINT_CLOSE_TAGS or e["code"] in PROGRESS_CODES or e["code"] in CANCEL_CODES
                 for e in later)
    # XML без action (разобраны до 11.11.2026): открытой считаем, только если после неё ничего не было
    if not closed and (s["action"] or later == []):
        # action — фраза для человека (подсказка дашборда), код ЕИС — action_code
        return {"since": s["at"][:10] or None, "xml_id": s["xml_id"],
                "action": "приостановлено определение поставщика" if s["action"] else None,
                "action_code": s["action"], "title": "Закупка приостановлена по жалобе"}
    return None


def _complaints_open(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Жалобы без решения, отзыва или возврата: по номеру; документы без номера — по порядку (закрывающий документ
    закрывает все поданные до него жалобы без номера)."""
    open_: Dict[str, Dict[str, Any]] = {}
    for i, e in enumerate(events):
        num = e.get("complaint_number")
        if e["tag"] in COMPLAINT_FILE_TAGS:
            open_[num or f"#{i}"] = e
        elif e["tag"] in COMPLAINT_CLOSE_TAGS and (e["code"] == "CMP-001" or e["tag"] != "checkResult"):
            if num and num in open_:
                open_.pop(num)
            elif not num:
                for k in [k for k in open_ if k.startswith("#")]:
                    open_.pop(k)
    if not open_:
        return None
    items = sorted(open_.values(), key=lambda e: e["at"])
    return {"count": len(items), "since": items[0]["at"][:10] or None,
            "numbers": [e["complaint_number"] for e in items if e.get("complaint_number")],
            "title": "Жалоба не рассмотрена" if len(items) == 1 else f"Жалоб без решения: {len(items)}"}


def _after_open(events: List[Dict[str, Any]], start: set, close: set, title: str) -> Optional[Dict[str, Any]]:
    """Последнее «открывающее» событие без «закрывающего» после него."""
    i = _last_index(events, lambda e: e["tag"] in start)
    if i < 0 or any(e["tag"] in close for e in events[i + 1:]):
        return None
    return {"since": events[i]["at"][:10] or None, "xml_id": events[i]["xml_id"], "title": title}


def flags(xmls: Sequence[Dict[str, Any]], stage: Optional[str] = None, checks: Sequence[Dict[str, Any]] = (),
          purchase_number: Optional[str] = None, rnp: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Наложенные состояния закупки (см. описание модуля).

    Args:
        xmls: XML закупки с событиями.
        stage: Стадия (:func:`stage`): у конечной стадии признаки без итога в ЕИС (предписание, претензии) не
            ставятся.
        checks: Проверки контрольных органов (``control_checks.normalize``).
        purchase_number: Номер закупки (поиск в списках проверок).
        rnp: Записи РНП по ИНН поставщика.

    Returns:
        Dict[str, Any]: Только сработавшие признаки: ``suspended``, ``complaint_open``, ``inspection_open``,
        ``prescription``, ``termination_pending``, ``claims``, ``rnp``.
    """
    events = timeline(xmls)
    final = stage in FINAL_STAGES
    out: Dict[str, Any] = {}
    susp = _suspended(events)
    if susp and not final:
        out["suspended"] = susp
    comp = _complaints_open(events)
    if comp and not final:
        out["complaint_open"] = comp
    insp = _after_open(events, INSPECTION_START_TAGS, INSPECTION_CLOSE_TAGS, "Идёт внеплановая проверка")
    if insp and not final:
        out["inspection_open"] = insp
    if not final and purchase_number:
        hits = [c for c in checks or [] if str(purchase_number) in (c.get("purchases") or ())
                and c.get("result") == "violations" and c.get("prescription")]
        if hits:
            last = max(hits, key=lambda c: str(c.get("date") or ""))
            out["prescription"] = {"since": str(last.get("date") or "")[:10] or None, "check_number": last.get("number"),
                                   "count": len(hits), "title": "Выдано предписание контрольного органа"}
    if stage not in ("terminated", "annulled", "completed"):
        term = _after_open(events, REFUSAL_TAGS, REFUSAL_CANCEL_TAGS | {"contractCancellation"},
                           "Односторонний отказ от исполнения контракта")
        if term and not any(e["code"] == "CON-007" for e in events if e["at"] >= (term["since"] or "")):
            out["termination_pending"] = term
    claims = [e for e in events if e["tag"] in CLAIM_TAGS]
    if claims and not final:
        out["claims"] = {"count": len(claims), "last": claims[-1]["at"][:10] or None,
                         "title": "Претензионная переписка по контракту"}
    basis = [e for e in events if e["code"] in EVASION_CODES or e["tag"] in REFUSAL_TAGS]
    if rnp:
        out["rnp"] = {"status": "included", "count": len(rnp), "title": "Поставщик в реестре недобросовестных поставщиков"}
    elif basis:
        out["rnp"] = {"status": "expected", "since": basis[-1]["at"][:10] or None,
                      "title": "Основание для включения в РНП (уклонение или односторонний отказ)"}
    return out

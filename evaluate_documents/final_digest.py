"""Вход итоговой проверки комплекта (``ConsistencyChecker``): компактная сводка и факты, посчитанные в коде.

Раньше модели отправлялся полный JSON всех документов, обрезанный до первых 14 500 токенов. В большом комплекте
(экспертиза 7185: 47 файлов, ~157 тыс. символов) модель видела только договор и часть актов и не знала ни НМЦК из
протокола, ни продления срока дополнительным соглашением — и выдавала противоречия, которых нет.

Теперь:

* :func:`compact_input` — по каждому файлу только то, что нужно для сверки (номер, суммы, даты, стороны), без
  фрагментов-доказательств и уверенности; если сводка всё равно велика, подробность снижается ступенями, а не
  обрезается хвост;
* :func:`package_facts` — сверка, которую надёжнее сделать кодом: номер договора (с учётом путаницы «З»/«3» при
  OCR), цена и НМЦК, срок с учётом дополнительных соглашений, дата последнего акта, сумма платёжных поручений,
  состояние ссылки на ЕИС.
"""
import json
import re
from collections import Counter
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from evaluate_documents.evidence_check import normalize_number, parse_date

CONTRACT_CODES = {"docContractDoWorkFiles", "docContractNIRFiles", "docContractPostTovarFiles", "docProjContractFiles"}
ACT_CODES = {"docDocPriemActSdachFiles", "docActPriemTovFiles", "docPriemTovSchetFiles"}
DS_CODES = {"docDopConsentContractFiles"}
# Документы, где номер договора должен совпадать с номером экспертизы
NUMBER_CHECK_CODES = CONTRACT_CODES | ACT_CODES | DS_CODES

PRICE_RE = re.compile(r"(цен\w*|сумм\w*|стоимост\w*)\s+(договор|контракт)", re.I)
NMCK_RE = re.compile(r"начальн\w*\s*\(?\s*максимальн|нмцк|н\(м\)цк", re.I)
END_RE = re.compile(r"окончани|заверш", re.I)
# Дата составления акта / справки о выполненных работах (КС-2, КС-3) или конец отчётного периода. Протоколы приёмочной
# комиссии и подписи не берутся: приёмка обычно не входит в срок выполнения работ
ACT_DATE_RE = re.compile(r"составлен\w*\s+(акт|справк)|отч[её]тн\w* период", re.I)
NOT_ACT_DATE_RE = re.compile(r"заключени|договор|контракт|начал|проверк|протокол|комисси|подпис", re.I)
PAYMENT_RE = re.compile(r"платеж|платёж", re.I)

# Ступени подробности сводки: (символов summary, элементов в списках, только имя для «Дополнительных материалов»)
DETAIL_LEVELS = ((300, 8, False), (0, 6, False), (0, 4, True), (0, 2, True))


def count_tokens(text: str) -> int:
    """Число токенов текста (``tiktoken``; без него — оценка «3 символа на токен»)."""
    try:
        from configs.utils import _ENCODER
        return len(_ENCODER.encode(text))
    except Exception:  # noqa: BLE001 — нет tiktoken/configs.utils в окружении
        return len(text) // 3 + 1


def _value(obj: Any) -> Any:
    """``value`` словаря или само значение."""
    return obj.get("value") if isinstance(obj, dict) else obj


def _money(value: Any) -> str:
    """Сумма в виде ``33 728 698,15``."""
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _files(data: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Пары (код типа документа, raw_data файла) по всему комплекту."""
    for doc in data.get("documents") or []:
        for raw in doc.get("raw_data") or []:
            if isinstance(raw, dict):
                yield doc.get("doc_code"), raw


def _is_payment(raw: Dict[str, Any]) -> bool:
    """Платёжное поручение (по имени файла или документа)."""
    return bool(PAYMENT_RE.search(f"{raw.get('filename') or ''} {_value(raw.get('document_name')) or ''}"))


def _file_digest(raw: Dict[str, Any], summary_chars: int, list_limit: int, name_only: bool) -> Dict[str, Any]:
    """Сводка одного файла."""
    out: Dict[str, Any] = {"file": raw.get("filename"), "name": _value(raw.get("document_name"))}
    for key in ("eis_status", "eis_error", "empty_comment"):
        if raw.get(key):
            out[key] = raw[key]
    if name_only:
        return {k: v for k, v in out.items() if v}
    if _value(raw.get("contract_number")):
        out["contract_number"] = _value(raw["contract_number"])
    amounts = [f"{f.get('context') or 'сумма'}: {_money(f.get('value'))}"
               for f in raw.get("finances") or [] if isinstance(f, dict) and f.get("value") not in (None, "")]
    if amounts:
        out["amounts"] = amounts[:list_limit]
    dates = [f"{d.get('context') or 'дата'}: {d.get('value')}"
             for d in raw.get("dates") or [] if isinstance(d, dict) and d.get("value")]
    if dates:
        out["dates"] = dates[:list_limit]
    parties = []
    for org in raw.get("organizations") or []:
        if isinstance(org, dict) and org.get("name"):
            inn = f", ИНН {org['inn']}" if org.get("inn") else ""
            parties.append(f"{org.get('role') or 'сторона'}: {org['name']}{inn}")
    if parties:
        out["parties"] = parties[:list_limit]
    nm = raw.get("nmck_method")
    if isinstance(nm, dict) and nm.get("method"):
        out["nmck_method"] = nm["method"]
    reports = [r.get("document_name") for r in raw.get("reporting_documentation") or []
               if isinstance(r, dict) and r.get("document_name")]
    if reports:
        out["reporting_documents"] = reports[:list_limit]
    if summary_chars and raw.get("summary"):
        out["summary"] = str(raw["summary"])[:summary_chars]
    return {k: v for k, v in out.items() if v}


def _doc_digest(doc: Dict[str, Any], level: Tuple[int, int, bool]) -> Dict[str, Any]:
    """Сводка одного типа документов: статусы проверок и файлы."""
    summary_chars, list_limit, dop_name_only = level
    out: Dict[str, Any] = {k: doc.get(k) for k in ("doc_code", "doc_type", "required", "empty_comment") if doc.get(k)}
    for key in ("type_compliance", "readability", "completeness"):
        ind = doc.get(key)
        if isinstance(ind, dict) and ind.get("status"):
            out[key] = f"{ind['status']}: {str(ind.get('description') or '')[:300]}".rstrip(": ")
    name_only = dop_name_only and doc.get("doc_code") == "docDopMaterialsFiles"
    out["files"] = [_file_digest(r, summary_chars, list_limit, name_only)
                    for r in doc.get("raw_data") or [] if isinstance(r, dict)]
    return out


def compact_input(data: Dict[str, Any], max_tokens: int = 14000) -> Tuple[Dict[str, Any], int]:
    """Компактная сводка комплекта для итоговой проверки.

    Args:
        data: Вход итоговой проверки (параметры экспертизы, ``missed_documents``, ``documents``).
        max_tokens: Желаемый предел сводки в токенах.

    Returns:
        tuple: (сводка, номер ступени подробности; ``len(DETAIL_LEVELS) - 1`` — самая краткая, даже если не уложилась).
    """
    head = {k: v for k, v in data.items() if k != "documents"}
    digest: Dict[str, Any] = {}
    for i, level in enumerate(DETAIL_LEVELS):
        digest = dict(head, documents=[_doc_digest(d, level) for d in data.get("documents") or []])
        if count_tokens(json.dumps(digest, ensure_ascii=False)) <= max_tokens:
            return digest, i
    return digest, len(DETAIL_LEVELS) - 1


def _most_common(values: List[float]) -> Optional[float]:
    """Самое частое значение (при равенстве — первое встреченное)."""
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def _amounts(raw: Dict[str, Any], pattern: re.Pattern, exclude: Optional[re.Pattern] = None) -> List[float]:
    """Суммы файла, у которых контекст подходит под ``pattern`` (и не подходит под ``exclude``)."""
    out = []
    for f in raw.get("finances") or []:
        ctx = str(f.get("context") or "") if isinstance(f, dict) else ""
        if isinstance(f, dict) and pattern.search(ctx) and not (exclude and exclude.search(ctx)):
            try:
                out.append(round(float(f.get("value")), 2))
            except (TypeError, ValueError):
                continue
    return out


def _dates(raw: Dict[str, Any], pattern: re.Pattern, exclude: Optional[re.Pattern] = None) -> List[date]:
    """Даты файла, у которых контекст подходит под ``pattern`` (и не подходит под ``exclude``)."""
    out = []
    for d in raw.get("dates") or []:
        if not isinstance(d, dict):
            continue
        ctx = str(d.get("context") or "")
        if pattern.search(ctx) and not (exclude and exclude.search(ctx)):
            dt = parse_date(d.get("value"))
            if dt:
                out.append(dt)
    return out


def package_facts(data: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """Сверка комплекта, посчитанная кодом по извлечённым (и проверенным по тексту) данным.

    Args:
        data: Вход итоговой проверки.
        today: Текущая дата (для тестов); даты из будущего не считаются датами актов.

    Returns:
        dict: Факты на русском — для промпта итоговой проверки. Отсутствующие сведения не включаются.
    """
    today = today or date.today()
    facts: Dict[str, Any] = {}
    files = list(_files(data))

    # Номер договора: сравнение с учётом путаницы символов при OCR («ЗА-8» и «3А-8» — один номер)
    expected = data.get("contract_number")
    if expected:
        norm = normalize_number(expected)
        differ, in_contract, matched = [], [], 0
        for code, raw in files:
            num = _value(raw.get("contract_number"))
            if not num or not (code in NUMBER_CHECK_CODES or _is_payment(raw)):
                continue
            if normalize_number(num) == norm:
                matched += 1
            else:
                (in_contract if code in CONTRACT_CODES else differ).append(f"{raw.get('filename')}: {num}")
        same = (f"совпадает с номером экспертизы «{expected}» в {matched} документ(ах) договора, актов, допсоглашений "
                f"и платёжных поручений (различия вида «З»/«3», «О»/«0» — ошибки распознавания, не противоречие)")
        if in_contract:
            facts["номер договора"] = (f"в договоре указан другой номер, чем в экспертизе «{expected}»: "
                                       f"{'; '.join(in_contract[:3])} — противоречие")
        elif differ:
            facts["номер договора"] = (f"{same if matched else 'номер экспертизы в документах не найден'}; в "
                                       f"{len(differ)} документ(ах) извлечён другой номер ({'; '.join(differ[:3])}) — "
                                       f"это может быть номер сметы, акта или счёта, требует проверки, но не является "
                                       f"подтверждённым противоречием")
        else:
            facts["номер договора"] = same

    # Цена договора и НМЦК
    price_values = [v for code, raw in files if code in CONTRACT_CODES for v in _amounts(raw, PRICE_RE, NMCK_RE)]
    if not price_values:   # договора в комплекте нет — цена из других документов (направление договора и т. п.)
        price_values = [v for _, raw in files for v in _amounts(raw, PRICE_RE, NMCK_RE)]
    price = _most_common(price_values)
    nmck_values = [v for _, raw in files for v in _amounts(raw, NMCK_RE)]
    nmck = _most_common(nmck_values)
    if price is not None:
        facts["цена договора"] = _money(price)
    if nmck is not None:
        others = sorted({_money(v) for v in nmck_values if v != nmck})
        facts["НМЦК"] = _money(nmck) + (f" (в отдельных документах также: {', '.join(others[:3])})" if others else "")
    if price is not None and nmck is not None:
        facts["цена и НМЦК"] = ("цена не превышает НМЦК" if price <= nmck + 0.01
                                else f"цена превышает НМЦК на {_money(price - nmck)}")
    elif price is not None:
        facts["цена и НМЦК"] = "НМЦК в документах не найдена — сравнение не выполнялось"

    # Сроки: договор и дополнительные соглашения
    contract_end = max((d for code, raw in files if code in CONTRACT_CODES for d in _dates(raw, END_RE)), default=None)
    ds_end = max((d for code, raw in files if code in DS_CODES for d in _dates(raw, END_RE)), default=None)
    deadline = max([d for d in (contract_end, ds_end) if d], default=None)
    if contract_end:
        facts["срок окончания работ по договору"] = contract_end.isoformat()
    if ds_end and (not contract_end or ds_end != contract_end):
        facts["срок окончания работ по допсоглашению"] = ds_end.isoformat()
    act_dates = [d for code, raw in files if code in ACT_CODES
                 for d in _dates(raw, ACT_DATE_RE, NOT_ACT_DATE_RE) if d <= today]
    if act_dates:
        last = max(act_dates)
        facts["последний акт/справка о выполненных работах"] = last.isoformat()
        if deadline:
            facts["сроки"] = (f"последний акт/справка о выполненных работах ({last.isoformat()}) не позже срока "
                              f"окончания работ ({deadline.isoformat()}) с учётом допсоглашений" if last <= deadline else
                              f"последний акт/справка о выполненных работах ({last.isoformat()}) позже срока "
                              f"окончания работ ({deadline.isoformat()}) — возможна просрочка")

    # Платёжные поручения: одна сумма на файл; без суммы хотя бы в одном — итог с ценой не сравнивается
    payments, no_amount = [], []
    for _, raw in files:
        if _is_payment(raw):
            vals = _amounts(raw, re.compile(r"платеж|платёж|сумм", re.I)) or _amounts(raw, re.compile(r"", re.S))
            if vals:
                payments.append(vals[0])
            else:
                no_amount.append(str(raw.get("filename")))
    if payments or no_amount:
        total = round(sum(payments), 2)
        line = f"{len(payments) + len(no_amount)} шт.; сумма по {len(payments)} с извлечённой суммой — {_money(total)}"
        if no_amount:
            line += (f"; в {len(no_amount)} сумма не извлечена ({'; '.join(no_amount[:3])}) — сравнение с ценой "
                     f"договора не выполнялось")
        elif price is not None:
            diff = round(total - price, 2)
            line += ("; совпадает с ценой договора" if abs(diff) <= 0.01 else
                     f"; {'больше' if diff > 0 else 'меньше'} цены договора на {_money(abs(diff))}")
        facts["платёжные поручения"] = line

    # Ссылка на ЕИС
    for code, raw in files:
        if code == "linkDocs" and raw.get("eis_status") and raw.get("eis_status") != "available":
            facts["ЕИС"] = ("документы закупки из ЕИС получить не удалось"
                            f" ({raw.get('eis_error') or raw.get('eis_status')}); содержимое ЕИС не проверено — "
                            "это ограничение проверки, а не нарушение в документах")
    return facts

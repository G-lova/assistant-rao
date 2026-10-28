"""Анализ закупки/контракта (этап S2–S3a): паспорт, комплект, согласованность, сводные риски, профиль.

Чистые функции без обращений к БД и модели — их собирает :class:`risk_monitoring.contract_analyzer.ContractAnalyzer`.

Источники рисков закупки (документы повторно не анализируются — берутся сохранённые результаты):

* **файлы** (``files.ai_analysis.risks``) — только действующие редакции (без ``superseded_by``); риски, которые
  аналитик отклонил (``status = 2``) или отметил устранёнными (``4``), не учитываются, подтверждённые (``1``)
  весят больше;
* **события XML** (``xml_sources.ai_analysis.risks``) — по последней версии каждого документа ЕИС;
* **правила внешней системы** (``risk_monitoring_evaluation_results.values``) — показатель > 0 → риск каталога
  с этим показателем (``pz_p5`` → FIN-003);
* **реестр 19.28 КоАП** (``zakupki_black_lists``): поставщик в реестре на дату контракта → REG-001;
* **данные закупки** (``dataset``): одна заявка → PROC-001, расторжение → CTR-020;
* **согласованность**: цена контракта и НМЦК в документах против ЕИС → DOC-009;
* **комплектность**: нет описания объекта, проекта контракта или обоснования НМЦК у конкурентной закупки → DOC-015.

Риски одного кода объединяются (``passport.merge_risks``): уровень — максимум, ссылки на источники — в
``evidence[].src`` (``file:<id>``, ``xml:<id>``, ``rule:<показатель>``, ``black_list:<id>``, ``eis``).
"""
import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from risk_monitoring import passport, risk_catalog

SCHEMA_VERSION = "rm-contract-1"

# Показатель внешней системы → код каталога (FIN-003 ↔ pz_p5 …)
INDICATOR_TO_CODE: Dict[str, str] = {
    rec["indicator"]: code for code, rec in risk_catalog.RISKS.items() if rec.get("indicator")
}
CODE_TO_INDICATOR: Dict[str, str] = {code: key for key, code in INDICATOR_TO_CODE.items()}
RULES_FZ = 44   # показатели внешней системы написаны для 44-ФЗ («Описание формул РИСК-МОНИТОРИНГА (44-ФЗ)»)


def rule_decision(code: str, values: Optional[Dict[str, Any]], fz: Any = None,
                  suspect: Iterable[str] = ()) -> Optional[str]:
    """Что говорит показатель внешней системы о риске каталога ``code``.

    Args:
        code: Код риска каталога.
        values: Значения показателей закупки (``risk_monitoring_evaluation_results.values``).
        fz: Закон закупки.
        suspect: Показатели с известными ошибками в формуле (``RULE_SUSPECT_INDICATORS``).

    Returns:
        Optional[str]: ``fired`` — сработал; ``not_fired`` — посчитан и не сработал; ``None`` — правило не решает:
        у кода нет показателя, расчёта нет, закупка не по 44-ФЗ (формулы написаны для 44-ФЗ) или формула ошибочна.
    """
    key = CODE_TO_INDICATOR.get(code)
    if not key or not values or key not in values or key in set(suspect or ()):
        return None
    law = risk_catalog.normalize_fz(fz)
    if law is not None and law != RULES_FZ:
        return None
    v = values.get(key)
    fired = v is True or (num(v) or 0) > 0
    return "fired" if fired else "not_fired"


# Ожидаемые документы конкурентной закупки: группа → допустимые типы (detected_type)
EXPECTED_DOCS = {
    "Описание объекта закупки": {"docOpusObjectZacupFiles", "docTechDocFiles"},
    "Проект контракта": {"docProjContractFiles", "docContractPostTovarFiles", "docContractDoWorkFiles",
                         "docContractNIRFiles"},
    "Обоснование НМЦК": {"docObosnNMCKFiles", "docMaterialValidNMCKFiles"},
}
CONTRACT_DOC_TYPES = {"docContractPostTovarFiles", "docContractDoWorkFiles", "docContractNIRFiles",
                      "docProjContractFiles"}
NMCK_DOC_TYPES = {"docObosnNMCKFiles", "docMaterialValidNMCKFiles"}
# Закупка у единственного поставщика (в т. ч. по ч. 12 ст. 93 — EZTP20) и иные неконкурентные способы:
# комплект документов не обязателен, «одна заявка» не признак низкой конкуренции
NONCOMPETITIVE_RE = re.compile(r"^(EP|ZP|SZ|EZT)|единствен|стать\w*\s+93|ст\.\s*93", re.I)


def competitive(method_code: Any, method: Any) -> Optional[bool]:
    """Конкурентный ли способ: ``True`` / ``False``; ``None`` — способ неизвестен (контракт без извещения в БД)."""
    code, name = str(method_code or "").strip(), str(method or "").strip()
    if not code and not name:
        return None
    return not (NONCOMPETITIVE_RE.search(code) or NONCOMPETITIVE_RE.search(name))

NMCK_CTX_RE = re.compile(r"нмц|начальн\w*\s*\(?\s*максимальн", re.I)
PRICE_CTX_RE = re.compile(r"цен\w*\s+(контракт|договор)|стоимост\w*\s+(контракт|договор)|сумм\w*\s+(контракт|договор)", re.I)

# Уровни детерминированных рисков закупки. Правила внешней системы — флаги 0/1 по одному полю ЕИС (например,
# pz_n1 срабатывает на любую закупку лекарств), поэтому их уровень умеренный: балл поднимают сочетания признаков
# Уровень риска от сработавшего правила внешней системы — в configs/settings.py (RULE_RISK_LEVEL, RULE_VIOLATION_LEVEL)
SINGLE_APPLICATION_LEVEL = 0.35
TERMINATION_LEVEL = 0.5

IGNORED_RISK_STATUSES = {2, 4}     # отклонён, устранён
CONFIRMED_STATUS = 1
REJECTED_STATUS = 2


# ------------------------------------------------------------------ значения dataset
def num(v: Any) -> Optional[float]:
    """Число из значения ``dataset`` (строки «12100920», «10.5», числа); иначе ``None``."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[  ]", "", str(v)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def flag(v: Any) -> Optional[bool]:
    """Логическое значение ``dataset`` («true» / «false» / 1 / 0); иначе ``None``."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "да", "yes"):
        return True
    if s in ("false", "0", "нет", "no"):
        return False
    return None


def as_json(v: Any) -> Any:
    """JSON-колонка из ответа внешней БД: строка разбирается, остальное — как есть."""
    if isinstance(v, (bytes, str)):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def as_date(v: Any) -> Optional[date]:
    """Дата из «2024-11-12T17:04:17+03:00», «2024-11-22+03:00», «2024-12-26»; иначе ``None``."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _g(d: Dict[str, Any], *path: str) -> Any:
    """Значение по пути в ``dataset`` (``None``, если чего-то нет)."""
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


# ------------------------------------------------------------------ паспорт
def stage(dataset: Dict[str, Any]) -> str:
    """Стадия закупки по ``dataset``: ``notice`` / ``contract`` / ``execution`` / ``completed`` / ``terminated``."""
    if _g(dataset, "procedure", "term_code") or _g(dataset, "procedure", "term_date"):
        return "terminated"
    if _g(dataset, "ispolnenie", "d_contr_fact") or flag(_g(dataset, "procedure", "final_stage")):
        return "completed"
    if _g(dataset, "procedure", "stage_start") or _g(dataset, "procedure", "paid_sum") or _g(dataset, "procedure", "acc_doc_num"):
        return "execution"
    if _g(dataset, "contract_details", "num_contr"):
        return "contract"
    return "notice"


def build_passport(card: Dict[str, Any], dataset: Dict[str, Any], files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Паспорт закупки/контракта — основа будущей экспертизы.

    Args:
        card: Строка ``risk_monitoring_contracts`` (``id``, ``number``, ``fz_type``, ``organisation_id``).
        dataset: ``dataset`` карточки.
        files: Файлы закупки (``id``, ``file_name``, ``detected_type``).

    Returns:
        Dict[str, Any]: Паспорт.
    """
    nmck, price = num(_g(dataset, "izvejenie", "nmck")), num(_g(dataset, "ispolnenie", "contr_zena"))
    return {
        "card_id": card.get("id"),
        "purchase_number": _g(dataset, "izvejenie", "izvech") or card.get("number"),
        "contract_number": _g(dataset, "contract_details", "num_contr"),
        "ikz": _g(dataset, "izvejenie", "ikz"),
        "law": f"{card.get('fz_type') or 44}-ФЗ",
        "fz": risk_catalog.normalize_fz(card.get("fz_type")),
        "stage": stage(dataset),
        "method_code": _g(dataset, "izvejenie", "vidz_code"),
        "method": _g(dataset, "izvejenie", "vidz"),
        "subject": _g(dataset, "contract_details", "predmet"),
        "okpd2": _g(dataset, "contract", "okpd2_code"),
        "okpd2_name": _g(dataset, "contract", "okpd2_name"),
        "nmck": nmck,
        "contract_price": price,
        "price_reduction_pct": round((1 - price / nmck) * 100, 2) if nmck and price and nmck > 0 else None,
        "winner_discount_pct": num(_g(dataset, "protocol", "win_discount_pct")),
        "applications": num(_g(dataset, "protocol", "app_count")),
        "single_application": flag(_g(dataset, "protocol", "single_app_flag")),
        "advance": _g(dataset, "izvejenie", "avans"),
        "security": {"application": _g(dataset, "izvejenie", "obesp_znach_zaivka"),
                     "contract": _g(dataset, "izvejenie", "obesp_znach_con"),
                     "warranty": _g(dataset, "izvejenie", "obesp_znach_gar")},
        "dates": {"published": _g(dataset, "izvejenie", "dt"), "applications_end": _g(dataset, "izvejenie", "dt_kon"),
                  "contract": _g(dataset, "contract_details", "contr_data"),
                  "execution_plan": _g(dataset, "ispolnenie", "d_contr_plan"),
                  "execution_fact": _g(dataset, "ispolnenie", "d_contr_fact")},
        "contract_status": _g(dataset, "contract_details", "contr_status"),
        "customer": {"organisation_id": card.get("organisation_id"), "inn": _g(dataset, "owner", "zak_inn"),
                     "kpp": _g(dataset, "owner", "zak_kpp"), "name": _g(dataset, "owner", "zak_name_krat"),
                     "region": _g(dataset, "owner", "region")},
        "supplier": {"inn": _g(dataset, "supplier", "post_inn"), "kpp": _g(dataset, "supplier", "post_kpp"),
                     "name": _g(dataset, "supplier", "post_name_krat"), "status": _g(dataset, "supplier", "post_stat")},
        "modification": {"code": _g(dataset, "contract", "mod_reason_code"), "name": _g(dataset, "contract", "mod_reason_name"),
                         "date": _g(dataset, "contract", "mod_doc_date")} if _g(dataset, "contract", "mod_reason_code") else None,
        "termination": {"code": _g(dataset, "procedure", "term_code"), "name": _g(dataset, "procedure", "term_name"),
                        "date": _g(dataset, "procedure", "term_date")} if _g(dataset, "procedure", "term_code") else None,
        "paid": num(_g(dataset, "procedure", "paid_sum")),
        "documents": [{"file_id": f.get("id"), "doc_type": f.get("detected_type"), "name": f.get("file_name"),
                       "current": not f.get("superseded_by")} for f in files],
    }


# ------------------------------------------------------------------ риски из источников
def _ev_src(risk: Dict[str, Any], src: str, **extra: Any) -> Dict[str, Any]:
    """Копия риска, у каждой цитаты которого указан источник (``file:<id>`` / ``xml:<id>``)."""
    r = dict(risk)
    evs = [dict(e, src=src, **extra) for e in (risk.get("evidence") or []) if isinstance(e, dict)]
    r["evidence"] = evs or [dict(src=src, **extra)]
    return r


def file_risks(files: Sequence[Dict[str, Any]], statuses: Dict[Tuple[int, str], int]) -> Tuple[List[Dict[str, Any]], set]:
    """Риски действующих редакций файлов без отклонённых и устранённых аналитиком.

    Args:
        files: Файлы (``id``, ``file_name``, ``risks``, ``superseded_by``).
        statuses: ``(file_id, rule_id) → status`` из ``risk_monitoring_document_risks``.

    Returns:
        Tuple[List[Dict[str, Any]], set]: Риски (``source = ai_file``) и коды, подтверждённые аналитиком.
    """
    from risk_monitoring.doc_kind import no_risk_by_name
    from risk_monitoring.document_risk_analyzer import rejected_by_rules

    out, confirmed = [], set()
    for f in files:
        if f.get("superseded_by"):
            continue
        # акт монтажа, смета, платёжка …: риски по тексту в них больше не ищутся, старый результат не учитывается
        skip_by_name = no_risk_by_name(f.get("file_name"))
        for r in as_json(f.get("risks")) or []:
            code = r.get("rule_id")
            st = statuses.get((int(f["id"]), code))
            if not code or st in IGNORED_RISK_STATUSES or (skip_by_name and st != CONFIRMED_STATUS):
                continue
            # файл мог быть проанализирован до уточнения правил отбора: риск, все цитаты которого правила
            # отбрасывают, в закупку не идёт (подтверждённый аналитиком — остаётся)
            evs = [e for e in (r.get("evidence") or []) if isinstance(e, dict) and e.get("fragment")]
            if st != CONFIRMED_STATUS and evs and all(
                    rejected_by_rules({"code": code, "title": r.get("title"), "explanation": r.get("description"),
                                       "evidence": {"fragment": e["fragment"]}}) for e in evs):
                continue
            if st == CONFIRMED_STATUS:
                confirmed.add(code)
            out.append(dict(_ev_src(r, f"file:{f['id']}", file_name=f.get("file_name")), source="ai_file"))
    return out, confirmed


def absence_claims(files: Sequence[Dict[str, Any]]) -> bool:
    """Есть ли среди рисков действующих файлов утверждение «в документе отсутствует …» об известной теме
    (оплата, приёмка, гарантии, обеспечение, ответственность) — тогда анализатору нужны тексты документов."""
    from risk_monitoring.document_risk_analyzer import absence_topics

    return any(absence_topics(r) for f in files if not f.get("superseded_by") for r in as_json(f.get("risks")) or [])


def drop_absence_contradicted(files: Sequence[Dict[str, Any]], texts: Dict[int, str],
                              statuses: Dict[Tuple[int, str], int]) -> int:
    """Снимает с файлов риски «отсутствует …», если названное есть в документах закупки.

    Закупка — комплект: раздел о гарантиях может быть в проекте контракта, а «нет раздела о гарантиях» модель
    находит в дополнительных условиях (1399, 20.10.2026: при этом в комплекте есть и «Гарантии.pdf»). Проверка
    по объединённому тексту всех документов; подтверждённые аналитиком риски не трогаются.

    Args:
        files: Файлы закупки (``risks`` изменяются на месте).
        texts: ``file_id → текст`` (документы без текста не участвуют).
        statuses: Статусы рисков у аналитика.

    Returns:
        int: Сколько рисков снято.
    """
    from risk_monitoring.document_risk_analyzer import absence_contradicted

    corpus = "\n".join(t for t in texts.values() if t)
    if not corpus:
        return 0
    dropped = 0
    for f in files:
        risks = as_json(f.get("risks")) or []
        keep = [r for r in risks if statuses.get((int(f["id"]), r.get("rule_id"))) == CONFIRMED_STATUS
                or not absence_contradicted(r, corpus)]
        if len(keep) < len(risks):
            dropped += len(risks) - len(keep)
            f["risks"] = keep
    return dropped


def _norm_fragment(e: Dict[str, Any]) -> str:
    """Цитата для сравнения: без пробельных различий, первые 300 символов."""
    return re.sub(r"\s+", " ", str(e.get("fragment") or "")).strip().lower()[:300]


def drop_same_quote(risks: Sequence[Dict[str, Any]], confirmed: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Риск модели, все цитаты которого уже приведены в более сильном риске, не учитывается отдельно.

    Модель ставит на одну цитату два кода (1387: устаревшие инструкции Госарбитража — DOC-006 и DOC-010; 1001:
    перечень телеканалов — DOC-002 и DOC-005), и балл растёт за один и тот же факт. Код-дубль переносится в
    ``related_codes`` сохранённого риска. Риски правил, событий и подтверждённые аналитиком не трогаются.

    Args:
        risks: Риски закупки после ``merge_risks`` (по убыванию значимости).
        confirmed: Коды, подтверждённые аналитиком.

    Returns:
        List[Dict[str, Any]]: Риски без дублей по цитатам.
    """
    keep_codes = set(confirmed)
    out: List[Dict[str, Any]] = []
    for r in sorted(risks, key=lambda x: -((x.get("level") or 0) * (x.get("confidence") or 0))):
        frags = {_norm_fragment(e) for e in r.get("evidence") or [] if isinstance(e, dict) and e.get("fragment")}
        only_ai = set(r.get("sources") or []) == {"ai_file"}
        if only_ai and frags and r.get("rule_id") not in keep_codes:
            host = next((k for k in out if set(k.get("sources") or []) & {"ai_file"}
                         and frags <= {_norm_fragment(e) for e in k.get("evidence") or [] if isinstance(e, dict)}),
                        None)
            if host is not None:
                host.setdefault("related_codes", []).append(r["rule_id"])
                continue
        out.append(r)
    order = {id(r): i for i, r in enumerate(risks)}
    return sorted(out, key=lambda r: order[id(r)])


def _xml_key(x: Dict[str, Any]) -> Tuple[str, str]:
    """Документ ЕИС, версиями которого являются строки XML: тег и номер."""
    return (str(x.get("xml_tag") or ""), str(x.get("purchase_number") or x.get("contract_number") or x.get("id")))


def latest_xml(xmls: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Последняя версия каждого документа ЕИС (по ``eis_version``, затем ``id``)."""
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for x in xmls:
        k = _xml_key(x)
        cur = best.get(k)
        rank = (num(x.get("eis_version")) or -1, int(x.get("id") or 0))
        if cur is None or rank > (num(cur.get("eis_version")) or -1, int(cur.get("id") or 0)):
            best[k] = x
    return list(best.values())


def event_risks(xmls: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Риски событий по последним версиям документов ЕИС (``source = ai_event`` / ``rule``)."""
    out = []
    for x in latest_xml(xmls):
        for r in as_json(x.get("risks")) or []:
            if r.get("rule_id"):
                src = (r.get("sources") or ["ai_event"])[0]
                out.append(dict(_ev_src(r, f"xml:{x['id']}"), source=src))
    return out


def events_feed(xmls: Sequence[Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
    """Лента событий закупки по всем версиям XML (новые — первыми)."""
    items = []
    for x in xmls:
        ev = as_json(x.get("event")) or {}
        if not ev:
            continue
        items.append({"xml_id": x.get("id"), "code": ev.get("code"), "title": ev.get("title"),
                      "importance": ev.get("importance"), "event_at": ev.get("event_at") or str(x.get("exact_date") or ""),
                      "eis_version": x.get("eis_version"), "summary": x.get("resume")})
    items.sort(key=lambda e: (str(e.get("event_at") or ""), int(e.get("xml_id") or 0)), reverse=True)
    return items[:limit]


def drop_rule_covered(risks: Sequence[Dict[str, Any]], values: Optional[Dict[str, Any]],
                      confirmed: Iterable[str] = (), fz: Any = None, suspect: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Риски модели по кодам, которые проверяет показатель внешней системы (FIN-004 ↔ pz_p1, FIN-005 ↔ pz_p2 …).

    По концепции ИИ работает поверх правил и не заменяет их (2.8, 2.9):

    * показатель посчитан и не сработал (44-ФЗ) — данные ЕИС утверждению модели противоречат, риск модели снимается
      (закупка 1399, 20.10.2026: «нет обеспечения исполнения» при pz_p2 = 0);
    * показатель сработал — находка модели остаётся подтверждением: её цитаты добавляются к риску правила, уровень —
      уровень правила (у подтверждения уровень 0, описание пустое), источник риска — «правило»;
    * правило не решает (223-ФЗ, нет расчёта, формула на проверке) — риск модели остаётся как есть («ИИ»).

    Подтверждённые аналитиком риски не трогаются.

    Args:
        risks: Риски файлов.
        values: Значения правил закупки.
        confirmed: Коды, подтверждённые аналитиком.
        fz: Закон закупки.
        suspect: Показатели с известными ошибками (``RULE_SUSPECT_INDICATORS``).

    Returns:
        List[Dict[str, Any]]: Риски модели без опровергнутых правилами; подтверждающие правило — как доказательства.
    """
    keep = set(confirmed)
    out = []
    for r in risks:
        code = r.get("rule_id")
        decision = None if code in keep else rule_decision(code, values, fz, suspect)
        if decision == "not_fired":
            continue
        if decision == "fired":
            r = {**r, "level": 0.0, "description": "", "law": None, "corroborates_rule": CODE_TO_INDICATOR[code]}
        out.append(r)
    return out


def rule_level(kind: Optional[str]) -> float:
    """Уровень риска сработавшего правила: нарушение — ``RULE_VIOLATION_LEVEL``, признак риска —
    ``RULE_RISK_LEVEL`` (по методике сводной аналитики нарушения весят больше рисков: много рисков при малом числе
    нарушений — жёлтая зона)."""
    from configs import settings

    return settings.RULE_VIOLATION_LEVEL if kind == "violation" else settings.RULE_RISK_LEVEL


def rule_values_risks(values: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Риски по сработавшим правилам внешней системы (значение показателя > 0 или истина).

    Args:
        values: ``{result_key: value}`` последнего расчёта.

    Returns:
        List[Dict[str, Any]]: Риски (``source = rule``, уверенность 1.0).
    """
    out = []
    for key, value in (values or {}).items():
        code = INDICATOR_TO_CODE.get(key)
        v = 1.0 if value is True else num(value)
        if not code or not v or v <= 0:
            continue
        kind = risk_catalog.kind(code)
        out.append({"rule_id": code, "title": risk_catalog.title(code),
                    "description": f"Сработал показатель {key} = {value}",
                    "level": rule_level(kind), "confidence": 1.0, "law": None,
                    "evidence": [{"src": f"rule:{key}", "value": value}],
                    "verification_needed": [], "source": "rule"})
    return out


def _add_years(d: date, years: int) -> date:
    """Дата через ``years`` лет (29 февраля → 28 февраля)."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def registry_matches(black: Sequence[Dict[str, Any]], supplier: Dict[str, Any], on: Optional[date]) -> List[Dict[str, Any]]:
    """Записи реестра 19.28 КоАП, действующие на дату (два года с ``date_begin``), для поставщика.

    Совпадение — по ИНН и, если в записи реестра и у поставщика указан КПП, по КПП.

    Args:
        black: Строки ``zakupki_black_lists`` с ИНН поставщика.
        supplier: ``inn``, ``kpp``.
        on: Дата (контракта; ``None`` — сегодня).

    Returns:
        List[Dict[str, Any]]: Действующие записи.
    """
    inn, kpp = str(supplier.get("inn") or "").strip(), str(supplier.get("kpp") or "").strip()
    on = on or date.today()
    out = []
    for b in black:
        if str(b.get("inn") or "").strip() != inn or not inn:
            continue
        if kpp and str(b.get("kpp") or "").strip() and str(b["kpp"]).strip() != kpp:
            continue
        begin = as_date(b.get("date_begin"))
        if begin and begin <= on < _add_years(begin, 2):
            out.append(b)
    return out


def registry_risks(matches: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """REG-001 по действующим записям реестра 19.28 КоАП."""
    if not matches:
        return []
    return [{"rule_id": "REG-001", "title": risk_catalog.title("REG-001"),
             "description": f"Поставщик в реестре лиц, привлечённых по ст. 19.28 КоАП РФ (дело {m.get('deal_number')}, "
                            f"с {str(m.get('date_begin'))[:10]})",
             "level": 0.9, "confidence": 1.0, "law": None,
             "evidence": [{"src": f"black_list:{m.get('id')}", "deal_number": m.get("deal_number"),
                           "date_begin": str(m.get("date_begin"))[:10], "sud": m.get("sud")}],
             "verification_needed": ["Проверить, распространяется ли запрет на дату заключения контракта"],
             "source": "registry"} for m in matches]


def _as_day(v: Any) -> Optional[date]:
    """Дата из даты/времени РНП (``2026-07-03 10:00:00.000`` → ``date``)."""
    return as_date(str(v)[:10]) if v not in (None, "") else None


def rnp_matches(entries: Sequence[Dict[str, Any]], supplier: Dict[str, Any], on: Optional[date]) -> List[Dict[str, Any]]:
    """Записи реестра недобросовестных поставщиков (``risk_monitoring_rnp_entries``), действующие на дату.

    Действующая — включение есть (``has_inclusion``), опубликована не позже даты, не исключена до неё (``excluded_at``)
    и плановая дата исключения не наступила. Совпадение — по ИНН и, если оба указаны, по КПП.

    Args:
        entries: Строки ``rnp_entries.sql`` с ИНН поставщика.
        supplier: ``inn``, ``kpp``.
        on: Дата (контракта; ``None`` — сегодня).

    Returns:
        List[Dict[str, Any]]: Действующие записи.
    """
    inn, kpp = str(supplier.get("inn") or "").strip(), str(supplier.get("kpp") or "").strip()
    on = on or date.today()
    out = []
    for e in entries:
        if not inn or str(e.get("inn") or "").strip() != inn:
            continue
        if kpp and str(e.get("kpp") or "").strip() and str(e["kpp"]).strip() != kpp:
            continue
        if not flag(e.get("has_inclusion")) and str(e.get("status") or "").lower() != "active":
            continue
        start = _as_day(e.get("first_published_at")) or _as_day(e.get("published_at"))
        end = _as_day(e.get("excluded_at")) or _as_day(e.get("planned_exclusion_date"))
        if (start is None or start <= on) and (end is None or on < end):
            out.append(e)
    return out


def rnp_risks(matches: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """REG-002 по действующим записям РНП (нарушение — только если заказчик установил требование ч. 1.1 ст. 31)."""
    return [{"rule_id": "REG-002", "title": risk_catalog.title("REG-002"),
             "description": f"Поставщик в реестре недобросовестных поставщиков (запись {m.get('reg_number')}, "
                            f"с {str(m.get('first_published_at') or m.get('published_at') or '')[:10]})",
             "level": 0.6, "confidence": 1.0, "law": None,
             "evidence": [{"src": f"rnp:{m.get('id')}", "reg_number": m.get("reg_number"), "fz": m.get("fz"),
                           "reason_code": m.get("reason_code"), "published_at": str(m.get("published_at") or "")[:10],
                           "planned_exclusion_date": str(m.get("planned_exclusion_date") or "")[:10] or None}],
             "verification_needed": ["Проверить, установлено ли в извещении требование об отсутствии участника в РНП "
                                     "(ч. 1.1 ст. 31 44-ФЗ / положение о закупке)"],
             "source": "registry"} for m in matches]


def dataset_risks(p: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Риски по данным ЕИС закупки: одна заявка → PROC-001, расторжение → CTR-020."""
    out = []
    if competitive(p.get("method_code"), p.get("method")) and (p.get("single_application") or p.get("applications") == 1):
        out.append({"rule_id": "PROC-001", "title": risk_catalog.title("PROC-001"),
                    "description": "На участие в конкурентной закупке подана одна заявка",
                    "level": SINGLE_APPLICATION_LEVEL, "confidence": 1.0, "law": None,
                    "evidence": [{"src": "eis", "path": "protocol.app_count", "value": p.get("applications")}],
                    "verification_needed": ["Проверить, не ограничивали ли требования документации круг участников"],
                    "source": "rule"})
    if p.get("termination"):
        t = p["termination"]
        out.append({"rule_id": "CTR-020", "title": risk_catalog.title("CTR-020"),
                    "description": f"Контракт расторгнут: {t.get('name') or t.get('code')} ({str(t.get('date') or '')[:10]})",
                    "level": TERMINATION_LEVEL, "confidence": 1.0, "law": None,
                    "evidence": [{"src": "eis", "path": "procedure.term_code", "value": t.get("code")}],
                    "verification_needed": ["Проверить основание расторжения и исполнение обязательств"],
                    "source": "rule"})
    return out


# ------------------------------------------------------------------ комплект и согласованность
def completeness(p: Dict[str, Any], files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Комплектность документов закупки по типам файлов.

    Проверяется только у конкурентной закупки с хотя бы одним проанализированным файлом: если файлов нет,
    комплект неизвестен (не скачан), а не неполон.

    Returns:
        Dict[str, Any]: ``checked``, ``expected``, ``present``, ``missing``.
    """
    types = {f.get("detected_type") for f in files if f.get("detected_type")}
    comp = competitive(p.get("method_code"), p.get("method"))
    if not comp or not types:
        return {"checked": False, "reason": "no_files" if comp else "noncompetitive" if comp is False else "unknown_method",
                "present": sorted(types)}
    missing = [name for name, allowed in EXPECTED_DOCS.items() if not (types & allowed)]
    return {"checked": True, "expected": list(EXPECTED_DOCS), "present": sorted(types), "missing": missing}


def completeness_risks(comp: Dict[str, Any], enabled: bool = True) -> List[Dict[str, Any]]:
    """DOC-015 по недостающим группам документов."""
    if not enabled or not comp.get("checked") or not comp.get("missing"):
        return []
    return [{"rule_id": "DOC-015", "title": risk_catalog.title("DOC-015"),
             "description": "В комплекте закупки не найдено: " + ", ".join(comp["missing"]),
             "level": 0.4, "confidence": 0.6, "law": None,
             "evidence": [{"src": "package", "missing": comp["missing"]}],
             "verification_needed": ["Проверить комплект в ЕИС: файл мог не скачаться или иметь другой тип"],
             "source": "package"}]


def _amounts(finances: Any, ctx_re: "re.Pattern", exclude_re: Optional["re.Pattern"] = None) -> List[float]:
    """Суммы из ``raw_data.finances`` файла с подходящим контекстом."""
    out = []
    for f in as_json(finances) or []:
        if not isinstance(f, dict):
            continue
        ctx = str(f.get("context") or "")
        v = num(f.get("value") if not isinstance(f.get("value"), dict) else f["value"].get("value"))
        if v and v > 0 and ctx_re.search(ctx) and not (exclude_re and exclude_re.search(ctx)):
            out.append(v)
    return out


def consistency(p: Dict[str, Any], files: Sequence[Dict[str, Any]], tolerance: float = 0.01) -> List[Dict[str, Any]]:
    """Сверка сумм документов с ЕИС: цена контракта (контракт / проект) и НМЦК (обоснование НМЦК).

    Расхождение — если ни одна сумма документа нужного контекста не совпадает с ЕИС с точностью ``tolerance``.

    Returns:
        List[Dict[str, Any]]: ``field``, ``eis``, ``documents`` (``file_id``, ``values``), ``issue``.
    """
    checks = [("contract_price", "Цена контракта", p.get("contract_price"), CONTRACT_DOC_TYPES, PRICE_CTX_RE, NMCK_CTX_RE),
              ("nmck", "НМЦК", p.get("nmck"), NMCK_DOC_TYPES, NMCK_CTX_RE, None)]
    out = []
    for field, label, eis, types, ctx_re, excl in checks:
        if not eis:
            continue
        for f in files:
            if f.get("superseded_by") or f.get("detected_type") not in types:
                continue
            # проект контракта до торгов содержит НМЦК, а не цену контракта — сверяем его только с НМЦК
            if field == "contract_price" and f.get("detected_type") == "docProjContractFiles" and p.get("stage") == "notice":
                continue
            vals = _amounts(f.get("finances"), ctx_re, excl)
            if not vals or any(abs(v - eis) <= tolerance * eis for v in vals):
                continue
            out.append({"field": field, "label": label, "eis": eis,
                        "documents": [{"file_id": f.get("id"), "file_name": f.get("file_name"), "values": vals[:5]}],
                        "issue": f"{label} в документе «{f.get('file_name')}» ({'; '.join(money(v) for v in vals[:3])}) "
                                 f"не совпадает с ЕИС ({money(eis)})"})
    return out


def consistency_risks(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DOC-009 «Несоответствие документа данным ЕИС» по расхождениям сумм."""
    return [{"rule_id": "DOC-009", "title": risk_catalog.title("DOC-009"), "description": c["issue"],
             "level": 0.6, "confidence": 0.7, "law": None,
             "evidence": [{"src": f"file:{d['file_id']}", "file_name": d.get("file_name"), "values": d["values"]}
                          for d in c["documents"]] + [{"src": "eis", "field": c["field"], "value": c["eis"]}],
             "verification_needed": ["Сверить сумму в документе и в ЕИС: возможна ошибка извлечения или иная редакция"],
             "source": "package"} for c in items]


# ------------------------------------------------------------------ профиль и балл
AI_SOURCES = {"ai_file", "ai_event", "version_diff"}


def _weight(r: Dict[str, Any], confirmed: Iterable[str], confirmed_weight: float, ai_weight: float = 1.0) -> float:
    """Вес риска в балле: ``level × confidence``; подтверждённый аналитиком × ``confirmed_weight``; найденный
    только моделью и не подтверждённый × ``ai_weight`` (его ещё нужно проверить); × ``decay_factor`` (затухание);
    не больше 1."""
    w = (passport.clamp01(r.get("level")) or 0.0) * (passport.clamp01(r.get("confidence")) or 0.0)
    if r.get("rule_id") in set(confirmed):
        w *= confirmed_weight
    elif r.get("sources") and set(r["sources"]) <= AI_SOURCES:
        w *= ai_weight
    # затухание во времени (концепция 10.10; risk_dynamics.apply_decay)
    w *= float(r.get("decay_factor") if r.get("decay_factor") is not None else 1.0)
    return min(1.0, w)


def decayed_or(weights: Iterable[float], decay: float) -> float:
    """«Шумовое ИЛИ» с затуханием: веса по убыванию, i-й умножается на ``decay ** i``.

    Обычное «ИЛИ» по многим средним рискам насыщается до 100 (закупка 1387: 9 рисков по 0,4–0,5 → 99,6).
    С затуханием главный риск задаёт балл, остальные добавляют всё меньше: 0,9 → 90; пять рисков по 0,5 → ≈ 69.
    """
    p = 1.0
    for i, w in enumerate(sorted(weights, reverse=True)):
        p *= 1 - w * (decay ** i)
    return 1 - p


def money(v: Optional[float]) -> str:
    """Сумма с пробелами между разрядами: 5 684 136,00."""
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") if v is not None else "—"


def zone(score: Optional[float], zones: Tuple[int, int] = (40, 70)) -> Optional[str]:
    """Зона балла: ``green`` / ``yellow`` / ``red``."""
    if score is None:
        return None
    return "green" if score < zones[0] else "yellow" if score < zones[1] else "red"


def profile(risks: Sequence[Dict[str, Any]], previous_score: Optional[float] = None, confirmed: Iterable[str] = (),
            confirmed_weight: float = 1.5, zones: Tuple[int, int] = (40, 70), decay: float = 0.5,
            ai_weight: float = 0.6) -> Dict[str, Any]:
    """Балл 0–100 «шумовым ИЛИ» с затуханием (:func:`decayed_or`), компоненты по категориям, зона, изменение и
    главные факторы. Риски, найденные только моделью и не подтверждённые аналитиком, весят ``ai_weight``.

    Args:
        risks: Сводные риски.
        previous_score: Прошлый балл (из текущего ``ai_analysis``).
        confirmed: Коды, подтверждённые аналитиком.
        confirmed_weight: Множитель подтверждённых.
        zones: Пороги зон.

    Returns:
        Dict[str, Any]: ``score``, ``zone``, ``delta``, ``previous_score``, ``components``, ``top_factors``.
    """
    confirmed = set(confirmed)
    weights = {id(r): _weight(r, confirmed, confirmed_weight, ai_weight) for r in risks}
    by_cat: Dict[str, List[float]] = {}
    for r in risks:
        by_cat.setdefault(risk_catalog.category(r.get("rule_id", "")), []).append(weights[id(r)])
    comps = {cat: decayed_or(ws, decay) for cat, ws in by_cat.items()}
    score = round(100 * decayed_or(weights.values(), decay), 1)
    top = sorted(risks, key=lambda r: weights[id(r)], reverse=True)[:5]
    return {
        "score": score, "zone": zone(score, zones),
        "previous_score": previous_score,
        "delta": round(score - previous_score, 1) if previous_score is not None else None,
        "components": {k: round(100 * v, 1) for k, v in sorted(comps.items())},
        "top_factors": [{"rule_id": r["rule_id"], "title": r.get("title"), "level": r.get("level"),
                         "confidence": r.get("confidence"), "confirmed": r["rule_id"] in confirmed,
                         "sources": r.get("sources"),
                         **({"rule_check": r["rule_check"].get("status")} if r.get("rule_check") else {}),
                         "refs": list(dict.fromkeys(e.get("src") for e in r.get("evidence") or [] if e.get("src")))[:5]}
                        for r in top],
    }


def input_hash(*parts: Any) -> str:
    """Хэш входных данных анализа: если он не изменился, закупку не пересчитываем."""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def default_resume(p: Dict[str, Any], prof: Dict[str, Any], risks: Sequence[Dict[str, Any]],
                   events: Sequence[Dict[str, Any]]) -> str:
    """Резюме закупки шаблоном (без модели)."""
    stage_names = {"notice": "определение поставщика", "contract": "контракт заключён", "execution": "исполнение",
                   "completed": "исполнен", "terminated": "расторгнут"}
    parts = [f"Закупка № {p.get('purchase_number')} ({p.get('law')}), «{(p.get('subject') or '')[:150]}»; "
             f"стадия: {stage_names.get(p.get('stage'), p.get('stage'))}."]
    if p.get("nmck"):
        parts.append(f"НМЦК {money(p['nmck'])} руб"
                     + (f", цена контракта {money(p['contract_price'])} руб" if p.get("contract_price") else "")
                     + (f" (снижение {p['price_reduction_pct']} %)" if p.get("price_reduction_pct") else "") + ".")
    parts.append(f"Балл риска {prof['score']} ({ {'green': 'зелёная', 'yellow': 'жёлтая', 'red': 'красная'}.get(prof['zone'], '') } зона)"
                 + (f", изменение {prof['delta']:+}" if prof.get("delta") else "") + ".")
    if risks:
        parts.append("Основные факторы: " + "; ".join(str(r.get('title') or r['rule_id']) for r in risks[:3]) + ".")
    if events:
        parts.append(f"Последнее событие: {events[0].get('title')} ({str(events[0].get('event_at') or '')[:10]}).")
    return " ".join(parts)


def favourite_supplier(p: Dict[str, Any], counts: Optional[Dict[str, int]], settings: Any) -> Optional[Dict[str, Any]]:
    """Доля поставщика закупки среди контрактов её заказчика за период и признак «любимого поставщика».

    «Любимый поставщик» — поставщик закупки является крупнейшим у заказчика, у него не меньше 3 контрактов и доля
    не меньше ``CUSTOMER_TOP_SUPPLIER_SHARE`` при не менее ``ORG_MIN_CONTRACTS`` контрактах заказчика.

    Args:
        p: Паспорт закупки.
        counts: ``ИНН поставщика → число контрактов заказчика за период`` (все строки заказчика по годам).
        settings: Пороги.

    Returns:
        Optional[Dict[str, Any]]: ``total``, ``supplier_contracts``, ``share``, ``rank``, ``favourite`` или ``None``.
    """
    inn = str((p.get("supplier") or {}).get("inn") or "").strip()
    if not inn or not counts:
        return None
    total = sum(counts.values())
    mine = int(counts.get(inn, 0))
    rank = 1 + sum(1 for k, v in counts.items() if k != inn and v > mine)
    share = round(mine / total, 3) if total else None
    favourite = bool(total >= settings.ORG_MIN_CONTRACTS and mine >= 3 and rank == 1
                     and (share or 0) >= settings.CUSTOMER_TOP_SUPPLIER_SHARE)
    return {"total": total, "supplier_contracts": mine, "share": share, "rank": rank, "suppliers": len(counts),
            "months": settings.ORG_PERIOD_MONTHS, "favourite": favourite}


def favourite_risks(p: Dict[str, Any], fav: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """BEH-006 «любимый поставщик» на уровне закупки."""
    if not fav or not fav.get("favourite"):
        return []
    sup = p.get("supplier") or {}
    return [{"rule_id": "BEH-006", "title": risk_catalog.title("BEH-006"),
             "description": f"Поставщик {sup.get('name') or sup.get('inn')} — крупнейший у заказчика: "
                            f"{fav['supplier_contracts']} из {fav['total']} контрактов за {fav['months']} мес. "
                            f"({round(100 * fav['share'])} %)",
             "level": 0.5, "confidence": 0.8, "law": None,
             "evidence": [{"src": f"customer:{(p.get('customer') or {}).get('inn')}", "supplier_inn": sup.get("inn"),
                           "supplier_contracts": fav["supplier_contracts"], "total": fav["total"], "share": fav["share"]}],
             "verification_needed": ["Проверить конкуренцию в закупках, где побеждает этот поставщик, и связи сторон"],
             "source": "aggregate"}]


def stored_registry(ai: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Совпадения с реестром 19.28 из ``ai_analysis`` закупки — и нового формата (список), и прежнего
    (``{"checked", "matches", "rnp"}``, результаты до 23.10.2026)."""
    reg = (ai or {}).get("registry")
    if isinstance(reg, dict):
        return list(reg.get("matches") or [])
    return list(reg or [])


def normalize_registry(ai: Dict[str, Any]) -> Dict[str, Any]:
    """Приводит ``registry`` сохранённого результата к формату API (список совпадений); ``checked`` и ``rnp``
    переносит в ``registry_checked`` и ``rnp``. Меняет ``ai`` на месте и возвращает его."""
    reg = (ai or {}).get("registry") if isinstance(ai, dict) else None
    if isinstance(reg, dict):
        ai["registry"] = list(reg.get("matches") or [])
        ai.setdefault("registry_checked", bool(reg.get("checked")))
        ai.setdefault("rnp", list(reg.get("rnp") or []))
    return ai


def build_contract_analysis(*, card: Dict[str, Any], files: Sequence[Dict[str, Any]], xmls: Sequence[Dict[str, Any]],
                            values: Optional[Dict[str, Any]], black: Sequence[Dict[str, Any]],
                            statuses: Dict[Tuple[int, str], int], previous_score: Optional[float] = None,
                            settings: Any = None, supplier_counts: Optional[Dict[str, int]] = None,
                            rnp: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Собирает ``ai_analysis`` закупки/контракта без модели (резюме добавляет анализатор).

    Args:
        card: Карточка (``id``, ``number``, ``fz_type``, ``organisation_id``, ``dataset``).
        files: Файлы закупки с кратким результатом анализа.
        xmls: XML закупки с событиями и рисками.
        values: Значения правил внешней системы.
        black: Записи реестра 19.28 по ИНН поставщика.
        statuses: Статусы рисков файлов у аналитика.
        supplier_counts: Контракты заказчика за период по ИНН поставщиков (для BEH-006); ``None`` — не проверять.
        rnp: Записи РНП по ИНН поставщика (``risk_monitoring_rnp_entries``) → REG-002.
        previous_score: Прошлый балл.
        settings: Модуль ``configs.settings`` (пороги).

    Returns:
        Dict[str, Any]: ``passport``, ``package``, ``registry``, ``events``, ``risks``, ``profile``, ``total_risk``,
        ``input_hash`` (служебный, переносится в ``pipeline``).
    """
    if settings is None:
        from configs import settings
    dataset = as_json(card.get("dataset")) or {}
    p = build_passport(card, dataset, files)
    fz = p["fz"]
    comp = completeness(p, files)
    cons = consistency(p, files, settings.CONSISTENCY_TOLERANCE)
    matches = registry_matches(black, p["supplier"], as_date(p["dates"]["contract"]))
    rnp_found = rnp_matches(rnp, p["supplier"], as_date(p["dates"]["contract"]))
    f_risks, confirmed = file_risks(files, statuses)
    file_ids = {int(f["id"]) for f in files if f.get("id") is not None}
    rejected = {code for (fid, code), st in statuses.items() if st == REJECTED_STATUS and fid in file_ids}
    f_risks = drop_rule_covered(f_risks, values, confirmed, fz, getattr(settings, "RULE_SUSPECT_INDICATORS", ()))
    fav = favourite_supplier(p, supplier_counts, settings)
    from risk_monitoring import purchase_profiles

    execution = purchase_profiles.execution_profile(dataset)
    risks = passport.merge_risks(
        rule_values_risks(values), f_risks, event_risks(xmls), registry_risks(matches), rnp_risks(rnp_found), dataset_risks(p),
        favourite_risks(p, fav), purchase_profiles.execution_risks(execution, settings),
        consistency_risks(cons), completeness_risks(comp, settings.COMPLETENESS_RISK), fz=fz)
    risks = drop_same_quote(risks, confirmed)
    prof = profile(risks, previous_score, confirmed, settings.CONFIRMED_WEIGHT, tuple(settings.SCORE_ZONES),
                   settings.SCORE_DECAY, settings.AI_UNCONFIRMED_WEIGHT)
    events = events_feed(xmls)
    files_brief = [{"file_id": f.get("id"), "file_name": f.get("file_name"), "detected_type": f.get("detected_type"),
                    "total_doc_risk": num(f.get("total_doc_risk")), "analyzed": f.get("rm_status") == "completed",
                    "superseded": bool(f.get("superseded_by")), "source": f.get("source")} for f in files]
    return {
        "passport": p,
        "package": {"completeness": comp, "consistency": cons, "files": files_brief,
                    "files_analyzed": sum(1 for f in files_brief if f["analyzed"]), "files_total": len(files_brief)},
        # API: registry — список совпадений с реестром 19.28 (у каждого black_list_id), 422 от 23.10.2026;
        # признак проверки и РНП — отдельными полями
        "registry": [{"black_list_id": m.get("id"), "deal_number": m.get("deal_number"),
                      "date_begin": str(m.get("date_begin"))[:10], "name": m.get("name")} for m in matches],
        "registry_checked": bool(p["supplier"].get("inn")),
        "rnp": [{"rnp_entry_id": m.get("id"), "reg_number": m.get("reg_number"), "fz": m.get("fz"),
                 "published_at": str(m.get("published_at") or "")[:10],
                 "planned_exclusion_date": str(m.get("planned_exclusion_date") or "")[:10] or None}
                for m in rnp_found],
        "customer_supplier": fav,
        # профиль исполнения контракта (purchase_profiles.execution_profile)
        "execution": execution,
        "events": events,
        "risks": risks,
        "total_risk": passport.total_risk(risks),
        "profile": prof,
        "confirmed_codes": sorted(confirmed),
        "rejected_codes": sorted(rejected),
        "input_hash": input_hash(p, [(r["rule_id"], r.get("level"), r.get("confidence")) for r in risks],
                                 [e.get("xml_id") for e in events], sorted(confirmed)),
    }

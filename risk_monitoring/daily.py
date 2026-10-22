"""Ежедневный прогон риск-мониторинга за дату выгрузки ЕИС (S0–S4) и сводка.

Этапы (каждый сам пропускает уже проанализированное — повторный прогон за ту же дату дешёвый):

* **S0 охват** — XML ЕИС архивов с ``exact_date = D`` (``queries/rm/daily_xml.sql``); их предыдущие версии и файлы
  — пакетно по выбранным XML;
* **S1b файлы** — ещё не проанализированные файлы этих XML (вложения, печатные формы) — ``AIFileAnalyzer``;
* **S1a события** — XML как события — ``XmlEventAnalyzer`` (с результатами файлов);
* **S2–S3a закупки** — затронутые закупки/контракты — ``ContractAnalyzer`` (документы уже проанализированы выше);
* **S3b заказчики** — заказчики затронутых закупок с ``monitoring = 1`` — ``OrgAnalyzer``;
* **S4 сводка** — по закупкам заказчиков на мониторинге: зоны, попавшие в красную зону, рост балла, важные события,
  реестр 19.28 и резюме (модель пересказывает готовые факты; без модели — шаблон).

Сводка хранится у нас (``ai_rm_results``, ``kind = daily``, ``external_id = ГГГГММДД``) и пишется во внешнюю БД —
``risk_monitoring_daily_runs`` (``POST /api/risk-monitoring/daily-runs``, ТЗ 09.10.2026; ``DAILY_RUNS_PUSH_ENABLED``).
Отдаётся и ``GET /risk-monitoring/daily/{date}``.

Сбой этапа не останавливает прогон: этап получает ``status = failed``, следующие работают с тем, что есть.
"""
import asyncio
import json
import time
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from configs.config import Config
from configs.logger import get_logger
from configs import settings
from risk_monitoring import passport, progress, rm_sql
from risk_monitoring.contract_analyzer import _none

logger = get_logger(__name__)

SCHEMA_VERSION = "rm-daily-1"
PROMPT_VERSION = "daily-resume-2"
KIND = "daily"
STAGES = ("files", "xml", "contracts", "customers", "suppliers", "summary")
RED = "red"
# «Выполняется» дольше этого — прогон считается оборванным (пересборка, падение воркера), его можно запустить заново
# Пульс идущего прогона: отметка «выполняется» обновляется у нас каждые HEARTBEAT_SECONDS; без обновления дольше
# RUNNING_STALE_SECONDS прогон считается оборванным (пересборка контейнера, падение воркера) — новый запуск за ту же
# дату не блокируется (раньше отметка без пульса блокировала повтор на 4 часа, 21.10.2026)
HEARTBEAT_SECONDS = 120
RUNNING_STALE_SECONDS = 10 * 60

RESUME_SYSTEM = (
    "Ты — аналитик риск-мониторинга госзакупок. Тебе дан список фактов ежедневной сводки за дату выгрузки ЕИС по "
    "заказчикам на мониторинге. Напиши сводку для руководителя: 4–6 предложений, по-русски, без вступлений — что "
    "пришло за сутки, что требует внимания в первую очередь (назови закупки и заказчиков из фактов), что проверить. "
    "Используй только эти факты: не добавляй цифр и выводов, которых в них нет. Если внимания ничего не требует — "
    "так и напиши. Не используй коды рисков и правил (вида DOC-002, FIN-004, BEH-005, pz_p1) — называй риски словами, понятными заказчику. Ответ — обычный текст, без JSON и разметки.")


def run_key(d: str) -> int:
    """id сводки в ``ai_rm_results``: дата ``ГГГГ-ММ-ДД`` → ``ГГГГММДД``."""
    return int(parse_date(d).strftime("%Y%m%d"))


def parse_date(d: Any) -> date:
    """Дата выгрузки из строки ``ГГГГ-ММ-ДД`` (или ``date`` / ``datetime``).

    Raises:
        ValueError: Строка не в формате ``ГГГГ-ММ-ДД``.
    """
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def _num(v: Any) -> Optional[float]:
    """Число или ``None``."""
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# Названия для заказчика там, где название каталога — профессиональный жаргон (аналитика организаций)
PLAIN_NAMES = {
    "BEH-001": "низкая конкуренция в закупках (одна заявка или единственный поставщик)",
    "BEH-002": "расторжения или систематический демпинг у поставщика",
    "BEH-005": "контракты сосредоточены у немногих поставщиков",
    "BEH-006": "большая доля контрактов у одного поставщика",
    "HIST-001": "одни и те же риски повторяются в закупках",
    "REG-001": "поставщик в реестре лиц, привлечённых по ст. 19.28 КоАП РФ",
    "REG-002": "поставщик в реестре недобросовестных поставщиков",
}


def risk_names(codes: Sequence[str]) -> str:
    """Названия рисков по каталогу вместо кодов: сводку читает заказчик («BEH-005» → «Аномальное распределение
    результатов»)."""
    from risk_monitoring import risk_catalog

    names = []
    for c in codes or []:
        t = PLAIN_NAMES.get(c) or risk_catalog.title(c) or "признак риска"
        if t not in names:
            names.append(t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t)
    return ", ".join(names)


def _sc(v: Optional[float]) -> str:
    """Балл без лишних нулей: 75.0 → «75», 66.5 → «66,5»."""
    return "—" if v is None else f"{v:g}".replace(".", ",")


def contract_item(card: Dict[str, Any], ai: Dict[str, Any], changed: bool) -> Dict[str, Any]:
    """Закупка для сводки: номер, заказчик, предмет, балл, зона, изменение балла, главные риски.

    Args:
        card: Строка ``daily_cards.sql``.
        ai: ``ai_analysis`` закупки.
        changed: Закупка пересчитана в этом прогоне (иначе ``delta`` относится к прошлому пересчёту и не показывается).
    """
    prof = ai.get("profile") or {}
    pas = ai.get("passport") or {}
    subject = pas.get("subject") or card.get("subject") or ""
    return {
        "card_id": int(card["id"]),
        "purchase_number": pas.get("purchase_number") or card.get("number"),
        "contract_number": pas.get("contract_number"),
        "customer": card.get("customer_name") or (pas.get("customer") or {}).get("name"),
        "organisation_id": card.get("organisation_id"),
        "subject": subject[:200],
        "score": _num(prof.get("score")),
        "zone": prof.get("zone"),
        "previous_score": _num(prof.get("previous_score")) if changed else None,
        "delta": _num(prof.get("delta")) if changed else None,
        "changed": changed,
        "top": [{"rule_id": f.get("rule_id"), "title": f.get("title")} for f in (prof.get("top_factors") or [])[:3]],
        "registry_1928": bool((ai.get("registry") or {}).get("matches")),
    }


def event_item(xml_id: int, card: Optional[Dict[str, Any]], ai: Dict[str, Any]) -> Dict[str, Any]:
    """Событие XML для сводки: тип, важность, закупка, резюме, риски."""
    ev = ai.get("event") or {}
    return {
        "xml_id": int(xml_id),
        "card_id": int(card["id"]) if card else None,
        "purchase_number": (card or {}).get("number"),
        "customer": (card or {}).get("customer_name"),
        "code": ev.get("code"),
        "title": ev.get("title"),
        "importance": ev.get("importance"),
        "event_at": ev.get("event_at"),
        "resume": str(ai.get("resume") or "")[:500],
        "risks": [r.get("rule_id") for r in ai.get("risks") or [] if r.get("rule_id")],
    }


def supplier_rows(cards: Dict[int, Dict[str, Any]], monitored_only: bool = True) -> List[Dict[str, Any]]:
    """Поставщики закупок прогона (юридические лица: ИНН 10 цифр, КПП 9 — так их принимает API организаций).

    Args:
        cards: ``card_id →`` строка ``daily_cards.sql`` (``supplier_inn``, ``supplier_kpp``, ``supplier_name``).
        monitored_only: Только закупки заказчиков на мониторинге.

    Returns:
        List[Dict[str, Any]]: ``inn``, ``kpp``, ``name``, ``short_name`` без повторов.
    """
    out: Dict[tuple, Dict[str, Any]] = {}
    for c in cards.values():
        if monitored_only and not monitored(c):
            continue
        inn, kpp = str(c.get("supplier_inn") or "").strip(), str(c.get("supplier_kpp") or "").strip()
        name = c.get("supplier_full_name") or c.get("supplier_name")
        if inn.isdigit() and len(inn) == 10 and kpp.isdigit() and len(kpp) == 9 and name:
            out.setdefault((inn, kpp), {"inn": inn, "kpp": kpp, "name": str(name), "short_name": c.get("supplier_name")})
    return list(out.values())


def monitored(card: Dict[str, Any]) -> bool:
    """Заказчик закупки на мониторинге (``monitoring = 1``; из MySQL приходит 1, 1.0, "1" или ``True``)."""
    try:
        return float(card.get("monitoring")) == 1
    except (TypeError, ValueError):
        return str(card.get("monitoring")).lower() == "true"


def choose_scope(total: int, cfg: Any = settings) -> str:
    """Охват анализа за дату: ``all`` — все XML; ``monitored`` — только закупки заказчиков на мониторинге
    (настройкой ``DAILY_SCOPE``); ``monitored_auto`` — XML больше ``DAILY_ALL_MAX_XML`` (массовая загрузка:
    2026-07-03 — 21 907 XML в 457 архивах при обычных 100–4000), анализ всех не уложится в сутки.

    Args:
        total: XML за дату.
        cfg: Настройки.

    Returns:
        str: ``all`` / ``monitored`` / ``monitored_auto``.
    """
    if cfg.DAILY_SCOPE == "monitored":
        return "monitored"
    limit = getattr(cfg, "DAILY_ALL_MAX_XML", None)
    return "monitored_auto" if limit and total > limit else "all"


def _stage_states(stages: Dict[str, Any]) -> Dict[str, Any]:
    """Этапы для ``risk_monitoring_daily_runs.stages``: статус, длительность и счётчики без текста ошибок."""
    return {k: {kk: vv for kk, vv in (v or {}).items() if kk != "error"} for k, v in (stages or {}).items()}


def slim_event(ai: Dict[str, Any]) -> Dict[str, Any]:
    """Из ``ai_analysis`` события — только то, что нужно сводке (за день бывают десятки тысяч событий)."""
    return {"event": ai.get("event"), "resume": str(ai.get("resume") or "")[:500], "link": ai.get("link"),
            "risks": [{"rule_id": r.get("rule_id")} for r in ai.get("risks") or [] if r.get("rule_id")]}


def slim_file(ai: Dict[str, Any]) -> Dict[str, Any]:
    """Из ``ai_analysis`` файла — только то, что нужно событиям (тип, резюме, риск документа)."""
    return {"doc_type": {"detected_type": (ai.get("doc_type") or {}).get("detected_type")}, "resume": ai.get("resume"),
            "total_doc_risk": ai.get("total_doc_risk")}


def _add_counts(acc: Dict[str, Any], counts: Dict[str, Any]) -> None:
    """Складывает счётчики пачек (списки ошибок записи — без повторов, не больше трёх)."""
    for k, v in counts.items():
        if isinstance(v, list):
            acc[k] = list(dict.fromkeys((acc.get(k) or []) + v))[:3]
        else:
            acc[k] = acc.get(k, 0) + v


def _importance_rank(v: Optional[str]) -> int:
    """Ранг важности события (``low`` → 0 … ``critical`` → 3)."""
    from risk_monitoring.risk_catalog import IMPORTANCE_ORDER

    return IMPORTANCE_ORDER.index(v) if v in IMPORTANCE_ORDER else -1


def build_summary(*, export_date: str, environment: str, cards: Dict[int, Dict[str, Any]],
                  contracts: Dict[int, Dict[str, Any]], changed: Iterable[int], events: Dict[int, Dict[str, Any]],
                  xml_cards: Dict[int, Optional[int]], customers: Sequence[Dict[str, Any]], scope: Dict[str, Any],
                  stages: Dict[str, Any], cfg: Any = settings,
                  suppliers: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Собирает сводку за дату без модели (резюме добавляет :class:`DailyRun`).

    Args:
        export_date: Дата выгрузки ``ГГГГ-ММ-ДД``.
        environment: Окружение.
        cards: ``card_id →`` строка ``daily_cards.sql`` (заказчик, ``monitoring``).
        contracts: ``card_id → ai_analysis`` закупки.
        changed: Закупки, пересчитанные в этом прогоне.
        events: ``xml_id → ai_analysis`` события.
        xml_cards: ``xml_id → card_id``.
        customers: Результаты ``OrgAnalyzer`` по заказчикам на мониторинге.
        suppliers: Результаты ``OrgAnalyzer`` по поставщикам их закупок (роль поставщика).
        scope: Счётчики охвата.
        stages: Состояние этапов.
        cfg: Настройки (``DAILY_*``, ``SCORE_ZONES``).

    Returns:
        Dict[str, Any]: ``scope``, ``stages``, ``contracts``, ``events``, ``customers``, ``attention``.
    """
    changed = set(changed)
    watched = {cid for cid, c in cards.items() if not cfg.DAILY_MONITORED_ONLY or monitored(c)}
    items = [contract_item(cards[cid], ai, cid in changed) for cid, ai in contracts.items() if cid in watched and cid in cards]
    by_zone = {z: sum(1 for i in items if i["zone"] == z) for z in ("green", "yellow", "red")}
    red_line = cfg.SCORE_ZONES[1]
    became_red = [i for i in items if i["zone"] == RED and i["changed"]
                  and (i["previous_score"] is None or i["previous_score"] < red_line)]
    jumps = sorted([i for i in items if (i["delta"] or 0) >= cfg.DAILY_SCORE_JUMP], key=lambda i: -(i["delta"] or 0))
    red = sorted([i for i in items if i["zone"] == RED], key=lambda i: -(i["score"] or 0))
    registry = [i for i in items if i["registry_1928"]]

    ev_items = [event_item(xid, cards.get(xml_cards.get(xid)) if xml_cards.get(xid) else None, ai)
                for xid, ai in events.items()
                if not cfg.DAILY_MONITORED_ONLY or (xml_cards.get(xid) in watched)]
    by_code: Dict[str, int] = {}
    for e in ev_items:
        key = e["title"] or e["code"] or "неизвестно"
        by_code[key] = by_code.get(key, 0) + 1
    important = sorted([e for e in ev_items if e["importance"] in cfg.DAILY_EVENT_IMPORTANCE or e["risks"]],
                       key=lambda e: (-_importance_rank(e["importance"]), -len(e["risks"]), e["xml_id"]))

    attention: List[Dict[str, Any]] = []
    seen = set()

    def add(kind: str, item: Dict[str, Any], text: str) -> None:
        """Пункт «внимания» без повторов по закупке и виду."""
        key = (kind, item.get("card_id"), item.get("xml_id"))
        if key not in seen:
            seen.add(key)
            attention.append({"kind": kind, "card_id": item.get("card_id"), "xml_id": item.get("xml_id"),
                              "purchase_number": item.get("purchase_number"), "customer": item.get("customer"),
                              "text": text})

    for i in registry:
        add("registry_1928", i, "Поставщик в реестре лиц, привлечённых по ст. 19.28 КоАП РФ")
    for i in became_red:
        if i["previous_score"] is None:
            # первая оценка закупки — «перешла в красную зону» вводило в заблуждение (21.10.2026: все 17 — первые)
            add("red_first", i, f"В красной зоне при первой оценке: балл {_sc(i['score'])}")
        else:
            add("became_red", i, f"Закупка перешла в красную зону: балл {_sc(i['score'])} (был {_sc(i['previous_score'])})")
    for i in jumps:
        add("score_jump", i, f"Балл вырос на {_sc(i['delta'])} — до {_sc(i['score'])}")
    for e in important[:cfg.DAILY_TOP]:
        add("event", e, f"{e['title'] or 'событие ЕИС'}" + (f"; признаки риска: {risk_names(e['risks'][:4])}" if e["risks"] else ""))

    cust = []
    for r in customers:
        sec = ((r.get("ai_analysis") or {}).get("customer")) or {}
        org = (r.get("ai_analysis") or {}).get("organisation") or {}
        if sec:
            cust.append({"organisation_id": r.get("organisation_id"), "canonical_id": r.get("canonical_id"),
                         "name": org.get("name"), "score": sec.get("score"), "zone": sec.get("zone"),
                         "delta": sec.get("delta"), "risks": [x.get("rule_id") for x in sec.get("risks") or []]})
    cust.sort(key=lambda c: -(c.get("score") or 0))
    sups = []
    for r in suppliers:
        sec = ((r.get("ai_analysis") or {}).get("supplier")) or {}
        org = (r.get("ai_analysis") or {}).get("organisation") or {}
        if sec:
            sups.append({"organisation_id": r.get("organisation_id"), "canonical_id": r.get("canonical_id"),
                         "name": org.get("name"), "inn": org.get("inn"), "score": sec.get("score"),
                         "zone": sec.get("zone"), "risks": [x.get("rule_id") for x in sec.get("risks") or []]})
            if any(x.startswith("REG-") for x in sups[-1]["risks"]):
                add("supplier_registry", {"card_id": None, "xml_id": None, "customer": org.get("name")},
                    risk_names([x for x in sups[-1]["risks"] if x.startswith("REG-")]))
    sups.sort(key=lambda c: -(c.get("score") or 0))
    top = cfg.DAILY_TOP
    return {
        "date": export_date,
        "environment": environment,
        "scope": {**scope, "contracts_monitored": len(items), "events_monitored": len(ev_items),
                  "customers_monitored": len({i["organisation_id"] for i in items if i.get("organisation_id")})},
        "stages": stages,
        "contracts": {"by_zone": by_zone, "changed": sum(1 for i in items if i["changed"]),
                      "became_red": became_red[:top], "score_jump": jumps[:top], "red": red[:top]},
        "events": {"total": len(ev_items), "by_type": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
                   "important": important[:top]},
        "customers": cust,
        "suppliers": sups,
        "attention": attention,
    }


def summary_facts(s: Dict[str, Any]) -> List[str]:
    """Факты сводки готовыми фразами — из них модель пишет текст, ничего не вычисляя сама.

    Args:
        s: Результат :func:`build_summary`.

    Returns:
        List[str]: Охват, зоны, внимание, заказчики, сбои этапов.
    """
    sc, c, ev = s["scope"], s["contracts"], s["events"]
    facts = [f"Выгрузка ЕИС за {s['date']}: документов XML {sc.get('xml', 0)}, файлов к анализу {sc.get('files', 0)}, "
             f"затронуто закупок {sc.get('contracts', 0)}, из них у заказчиков на мониторинге "
             f"{sc.get('contracts_monitored', 0)} (заказчиков: {sc.get('customers_monitored', 0)})."]
    if not sc.get("xml") and sc.get("nearest_exports") is not None:
        near = [n for n in sc["nearest_exports"] if n.get("xml")]
        facts.append(f"Выгрузок ЕИС с датой {s['date']} нет (дата прогона — день выгрузки, а не публикации документов)"
                     + ("; ближайшие выгрузки с XML: " + ", ".join(f"{n['date']} (XML {n['xml']})" for n in near[:6])
                        if near else "; в соседние ±7 дней выгрузок с XML тоже нет") + ".")
    if sc.get("scope_mode") == "monitored_auto":
        facts.append(f"Выгрузка большая (XML {sc.get('xml_total')}): проанализированы только документы закупок заказчиков "
                     "на мониторинге, остальные — нет.")
    if not sc.get("contracts_monitored") and not ev["total"]:
        facts.append("По заказчикам на мониторинге за эту дату новых документов нет.")
    else:
        z = c["by_zone"]
        facts.append(f"Закупки заказчиков на мониторинге по зонам риска: красная {z['red']}, жёлтая {z['yellow']}, "
                     f"зелёная {z['green']}; пересчитано в этом прогоне {c['changed']}.")
        if ev["total"]:
            types = "; ".join(f"{k} — {v}" for k, v in list(ev["by_type"].items())[:6])
            facts.append(f"Событий ЕИС по ним: {ev['total']} ({types}).")
    kinds = {}
    for a in s["attention"]:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    if kinds:
        names = {"registry_1928": "поставщик в реестре 19.28", "became_red": "перешли в красную зону",
                 "red_first": "в красной зоне при первой оценке", "score_jump": "резкий рост балла",
                 "event": "важные события", "supplier_registry": "поставщики в реестрах"}
        facts.append("Требуют внимания (всего " + str(len(s["attention"])) + "): " +
                     "; ".join(f"{names.get(k, k)} — {v}" for k, v in kinds.items()) +
                     ". Ниже перечислены первые из них; называй числа из этой строки, а не считай пункты.")
    for a in s["attention"][:10]:
        who = ", ".join(x for x in (f"закупка {a['purchase_number']}" if a.get("purchase_number") else None,
                                    a.get("customer")) if x)
        facts.append(f"Внимание: {a['text']}" + (f" — {who}" if who else "") + ".")
    if not s["attention"]:
        facts.append("Закупок, перешедших в красную зону, резкого роста балла, важных событий и поставщиков из "
                     "реестра 19.28 нет.")
    risky = [x for x in s["customers"] if x.get("risks")]
    if risky:
        facts.append("Заказчики с признаками риска: " + "; ".join(
            f"{x['name']} — балл {_sc(x['score'])} ({risk_names(x['risks'][:3])})" for x in risky[:5]) + ".")
    partial = {k: v for k, v in (s.get("stages") or {}).items() if (v or {}).get("status") == "partial"}
    if partial:
        left = max((v or {}).get("xml_left") or 0 for v in partial.values())
        facts.append(f"Прогон не успел обработать все документы (осталось XML: {left}) — сводка предварительная, "
                     "прогон за эту дату нужно повторить.")
    failed = [k for k, v in (s.get("stages") or {}).items() if (v or {}).get("status") == "failed"]
    if failed:
        facts.append("Не завершились этапы: " + ", ".join(failed) + " — сводка может быть неполной.")
    return facts


def default_resume(s: Dict[str, Any]) -> str:
    """Текст сводки шаблоном (без модели)."""
    return " ".join(summary_facts(s))


class DailyRun:
    """Ежедневный прогон за дату выгрузки.

    Args:
        http_manager: ``HTTPClientManager``.
        environment: Окружение внешней системы.
        repo: ``AIRepo`` (наше хранилище результатов и сводок).
        push_results: Записывать результаты этапов во внешнюю БД.
        use_llm: Резюме (закупок, организаций, сводки) пишет модель.
        stages: Какие этапы выполнять (по умолчанию все ``STAGES``).
    """

    def __init__(self, http_manager: Any, environment: str, repo: Any, push_results: bool = True,
                 use_llm: bool = True, stages: Optional[Sequence[str]] = None):
        """Создаёт доступ к внешней БД."""
        from configs.data_fetcher import DataFetcher

        cfg = Config()
        db = cfg.get_database_config(environment)
        self.http_manager = http_manager
        self.environment = environment
        self.env = (environment or cfg.APP_ENV).lower()
        self.storage_path = db.storage_path
        self.repo = repo
        self.push = push_results
        self.use_llm = use_llm
        self.stages_on = tuple(s for s in (stages or STAGES) if s in STAGES) or STAGES
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)
        self.stages: Dict[str, Any] = {}
        self.llm_stats: Dict[str, Any] = {}

    async def _query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Выполняет ``queries/rm/<name>``."""
        sql, bindings = rm_sql.render(name, head=head, **lists)
        df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [{k: _none(v) for k, v in r.items()} for r in df.to_dict("records")] if not df.empty else []

    async def _stage(self, name: str, coro_fn: Any) -> Any:
        """Выполняет этап: время, счётчики, сбой — ``status = failed`` без остановки прогона."""
        if name not in self.stages_on:
            self.stages[name] = {"status": "skipped"}
            return None
        start = time.monotonic()
        try:
            async with progress.step(f"этап «{name}»"):
                out, counts = await coro_fn()
            self.stages[name] = {"status": "completed", "seconds": round(time.monotonic() - start), **(counts or {})}
            return out
        except Exception as e:  # noqa: BLE001 — сбой этапа не останавливает прогон
            logger.error(f"Ежедневный прогон, этап {name}: {type(e).__name__}: {e}", exc_info=True)
            self.stages[name] = {"status": "failed", "seconds": round(time.monotonic() - start),
                                 "error": f"{type(e).__name__}: {e}"[:500]}
            return None

    @staticmethod
    def _counts(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Число результатов по статусам и записи во внешнюю БД (``pushed`` / ``push_failed`` и первые ошибки).

        Анализ мог пройти, а запись — нет (прогон 21.10.2026: закупок 76 «done», во внешней БД — 4): без этих
        счётчиков сводка показывала успех.
        """
        out: Dict[str, Any] = {"total": len(results)}
        errors: List[str] = []
        for r in results:
            out[r.get("status", "unknown")] = out.get(r.get("status", "unknown"), 0) + 1
            if r.get("pushed"):
                out["pushed"] = out.get("pushed", 0) + 1
            elif r.get("push_error"):
                out["push_failed"] = out.get("push_failed", 0) + 1
                err = str(r["push_error"])[:300]
                if err not in errors and len(errors) < 3:
                    errors.append(err)
        if errors:
            out["push_errors"] = errors
        return out

    async def _internal(self, kind: str, ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        """Наши сохранённые результаты (``ai_rm_results``) — ``id → ai_analysis`` завершённых."""
        if not ids or not hasattr(self.repo, "get_rm_results"):
            return {}
        try:
            rows = await self.repo.get_rm_results(self.env, kind, list(ids))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Сохранённые результаты ({kind}) не получены: {type(e).__name__}: {e}")
            return {}
        return {i: r["ai_analysis"] for i, r in rows.items() if r.get("ai_analysis")}

    async def save(self, export_date: str, ai: Dict[str, Any]) -> None:
        """Сохраняет сводку (или отметку «выполняется») у нас и в ``risk_monitoring_daily_runs`` внешней БД.

        Внешняя БД (API daily-runs, 21.10.2026): ``POST /api/risk-monitoring/daily-runs`` — состояние прогона
        (статус, этапы, время с часовым поясом; upsert по дате выгрузки, ответ — ``id``), затем для готовой сводки —
        ``PATCH /api/risk-monitoring/ai-analysis`` с ``table = risk_monitoring_daily_runs`` и этим ``id``. Включается
        ``DAILY_RUNS_PUSH_ENABLED`` и записью результатов прогона; сбой записи не останавливает прогон — сводка у нас
        остаётся, ``pushed`` / ``push_error`` в ``ai_rm_results`` показывают, дошла ли она.
        """
        async with self._save_lock():
            await self._save(export_date, ai)
            if (ai.get("pipeline") or {}).get("status") not in (None, "running"):
                self._finished = True

    async def _save(self, export_date: str, ai: Dict[str, Any]) -> None:
        """Запись сводки или отметки (под блокировкой :meth:`save`)."""
        pipe = ai.get("pipeline") or {}
        pushed, error = False, None
        if self.push and settings.DAILY_RUNS_PUSH_ENABLED and self.http_manager is not None:
            try:
                api = self.__dict__.get("risk_api")
                if api is None:
                    from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

                    api = self.risk_api = RiskMonitoringAPI(self.http_manager, self.environment)
                status = pipe.get("status") or "running"
                done = status in ("completed", "failed", "partial")
                row = await api.upsert_daily_run(
                    export_date, status, stages=_stage_states(ai.get("stages")) if ai.get("stages") else None,
                    started_at=passport.iso_tz(pipe["started_at"]) if pipe.get("started_at") else None,
                    finished_at=passport.iso_tz(pipe["finished_at"]) if done and pipe.get("finished_at") else None)
                if done and row.get("id") is not None:
                    await api.patch_ai_analysis("risk_monitoring_daily_runs", int(row["id"]), ai)
                pushed = True
            except Exception as e:  # noqa: BLE001 — сводка остаётся у нас
                error = f"{type(e).__name__}: {e}"[:500]
                logger.warning(f"Сводка {export_date} не записана в risk_monitoring_daily_runs: {error}")
        if hasattr(self.repo, "save_rm_result"):
            await self.repo.save_rm_result(self.env, KIND, run_key(export_date), ai, pushed, error)

    async def run(self, export_date: str) -> Dict[str, Any]:
        """Выполняет этапы за дату и возвращает сводку.

        Args:
            export_date: Дата выгрузки ``ГГГГ-ММ-ДД``.

        Returns:
            Dict[str, Any]: Сводка (``ai_analysis`` с ``kind = daily``).
        """
        from risk_monitoring.llm_json import CALLS

        d = parse_date(export_date).isoformat()
        before = dict(CALLS)
        started = passport.now_iso()
        progress.subject(f"Ежедневный прогон {d} ({self.env})")
        # история запусков за дату: started_at / finished_at строки — последнего запуска, прежние — в pipeline.runs
        prev = (await self._internal(KIND, [run_key(d)])).get(run_key(d)) or {}
        self.runs = previous_runs(prev)
        marker = {"date": d, "pipeline": {"schema_version": SCHEMA_VERSION, "stage": "S0-S4", "status": "running",
                                          "started_at": started, "heartbeat_at": started, "runs": self.runs,
                                          "attempt": len(self.runs) + 1}}
        await self.save(d, marker)
        beat = asyncio.ensure_future(self._heartbeat(d, marker))
        try:
            return await self._run_from_s0(d, started, before)
        except Exception as e:
            # прогон упал целиком (например, 401 внешней БД на S0): отметка «выполняется» не должна висеть —
            # сохраняем failed с причиной, повторный запуск за дату не блокируется
            failed = {"date": d, "pipeline": {**marker["pipeline"], "status": "failed", "finished_at": passport.now_iso(),
                                              "error": f"{type(e).__name__}: {str(e)[:500]}"}}
            failed["pipeline"]["runs"] = self.runs + [{"started_at": started, "finished_at": failed["pipeline"]["finished_at"],
                                                       "status": "failed"}]
            try:
                await self.save(d, failed)
            except Exception as se:  # noqa: BLE001
                logger.warning(f"Отметка о сбое прогона {d} не сохранена: {type(se).__name__}: {se}")
            raise
        finally:
            beat.cancel()

    async def _heartbeat(self, d: str, marker: Dict[str, Any]) -> None:
        """Пока прогон идёт — обновляет у нас ``pipeline.heartbeat_at`` отметки «выполняется» (во внешнюю БД не
        пишет). Итоговая сводка под той же блокировкой: пульс её не перезапишет."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            async with self._save_lock():
                if self.__dict__.get("_finished") or not hasattr(self.repo, "save_rm_result"):
                    return
                marker["pipeline"]["heartbeat_at"] = passport.now_iso()
                try:
                    await self.repo.save_rm_result(self.env, KIND, run_key(d), marker, False, None)
                except Exception as e:  # noqa: BLE001 — пульс не обязателен
                    logger.warning(f"Пульс прогона {d} не записан: {type(e).__name__}: {e}")

    def _save_lock(self) -> asyncio.Lock:
        """Блокировка записи отметки и сводки (создаётся в event loop прогона)."""
        lock = self.__dict__.get("_lock")
        if lock is None:
            lock = self._lock = asyncio.Lock()
        return lock

    async def _run_from_s0(self, d: str, started: str, before: Dict[str, int]) -> Dict[str, Any]:
        """S0 и дальнейшие этапы (см. :meth:`run`)."""
        async with progress.step("S0: XML за дату выгрузки") as st:
            xml_rows = await self._query("daily_xml.sql", head=[self.storage_path, d])
            total = len(xml_rows)
            mode = choose_scope(total, settings)
            if mode != "all":
                xml_rows = [r for r in xml_rows if monitored(r)]
            xml_ids = [int(r["id"]) for r in xml_rows]
            st["detail"] = f"XML {len(xml_ids)}" + (f" из {total} (охват {mode})" if mode != "all" else "")
        scope: Dict[str, Any] = {"xml": len(xml_ids), "files": 0, "contracts": 0, "xml_total": total,
                                 "scope_mode": mode}
        if not xml_ids:
            scope["nearest_exports"] = await self._nearest(d)
            progress.note(f"за дату выгрузки {d} XML нет; выгрузки рядом: {scope['nearest_exports']}")
        file_results: Dict[int, Dict[str, Any]] = {}

        from risk_monitoring.run_cache import RunCache

        # общий кэш прогона: каждый XML, его предыдущая версия, файлы и архив ЕИС — один раз для обоих анализаторов
        cache = self.cache = RunCache(self._make_archives())
        if xml_ids:
            await self._seed(d, xml_rows, xml_ids, cache)
        try:
            return await self._run(d, xml_rows, xml_ids, scope, file_results, cache, started, before)
        finally:
            cache.archives.close()

    async def _seed(self, d: str, xml_rows: List[Dict[str, Any]], xml_ids: List[int], cache: Any) -> None:
        """Сведения о XML (из запроса S0), их предыдущие версии и файлы — в кэш прогона, пакетно по выбранным XML:
        анализаторы файлов и событий берут их оттуда, не обращаясь к внешней БД.

        Только по XML, которые будут анализироваться (не по всей дате): запрос предыдущих версий по всей дате —
        соединение таблицы XML с собой — на выгрузке 2026-07-03 (21 907 XML) не уложился в таймаут шлюза (504),
        хотя анализировать нужно было 111 XML. Сбой не останавливает прогон: недостающее анализаторы дозапросят.
        """
        from risk_monitoring.run_cache import chunks

        async with progress.step("S0: предыдущие версии и файлы выбранных XML (пакетно)") as st:
            cache.seed("xml_meta.sql", xml_rows, "id", xml_ids)
            try:
                prev = await cache.keyed(self._query, "xml_previous.sql", xml_ids, "current_id", head=[self.storage_path])
                prev_rows = [r for rows in prev.values() for r in rows]
                linked = {int(r["id"]) for r in xml_rows if r.get("risk_monitoring_contract_id") is not None}
                cards = sorted({int(r["risk_monitoring_contract_id"]) for r in xml_rows if int(r["id"]) in linked})
                files: List[Dict[str, Any]] = []
                id_parts, card_parts = chunks(xml_ids), chunks(cards)
                for n in range(max(len(id_parts), len(card_parts))):
                    cache.count("daily_files.sql")
                    files += await self._query("daily_files.sql", ids=id_parts[n] if n < len(id_parts) else [],
                                               cards=card_parts[n] if n < len(card_parts) else [])
                # файлы предыдущих версий привязанных XML — среди файлов тех же закупок
                prev_linked = [int(p["id"]) for p in prev_rows if p.get("id") is not None
                               and _num(p.get("current_id")) is not None and int(_num(p["current_id"])) in linked]
                cache.seed_files(files, list(xml_ids) + prev_linked, cards)
                cache.cover_xml(list(linked) + prev_linked)
                st["detail"] = f"XML {len(xml_ids)}, предыдущих версий {len(prev_rows)}, файлов {len(files)}"
            except Exception as e:  # noqa: BLE001 — анализаторы запросят сами
                logger.warning(f"Ежедневный прогон {d}: предварительная загрузка не выполнена ({type(e).__name__}: "
                               f"{str(e)[:200]}) — анализаторы запросят данные сами")
                st["detail"] = "не выполнена"
            cache.queries["daily_xml.sql"] = 1

    async def _documents(self, xml_ids: List[int], xa: Any, cache: Any, scope: Dict[str, Any],
                         file_results: Dict[int, Dict[str, Any]], events: Dict[int, Dict[str, Any]]) -> None:
        """S1b файлы и S1a события пачками по ``DAILY_XML_BATCH`` XML: память под разобранные XML освобождается
        после каждой пачки (в выгрузке за день бывает больше 20 000 XML). Сбой пачки не останавливает прогон.

        Args:
            xml_ids: XML за дату.
            xa: ``XmlEventAnalyzer`` с общим кэшем.
            cache: ``RunCache``.
            scope: Счётчики охвата (``files``).
            file_results: Результаты файлов (сокращённые) для событий — дополняются.
            events: ``xml_id → сокращённый ai_analysis`` события — дополняется.
        """
        from risk_monitoring.run_cache import chunks

        st_files = {"status": "completed", "seconds": 0, "total": 0}
        st_xml = {"status": "completed", "seconds": 0, "total": 0}
        if "files" not in self.stages_on:
            st_files = {"status": "skipped"}
        if "xml" not in self.stages_on:
            st_xml = {"status": "skipped"}
        fa = None
        parts = chunks(xml_ids, max(1, int(settings.DAILY_XML_BATCH)))
        deadline = time.monotonic() + settings.DAILY_DOCUMENTS_BUDGET_SEC
        for n, part in enumerate(parts, 1):
            if time.monotonic() > deadline:
                # бюджет времени на документы исчерпан: остальное — следующим запуском за ту же дату (уже
                # проанализированное пропускается), а этот прогон успевает собрать закупки и сводку до предела задачи
                left = len(parts) - n + 1
                for st in (st_files, st_xml):
                    if st.get("status") == "completed":
                        st.update(status="partial", batches_left=left, xml_left=sum(len(p) for p in parts[n - 1:]))
                logger.warning(f"Ежедневный прогон: бюджет {settings.DAILY_DOCUMENTS_BUDGET_SEC} с на документы "
                               f"исчерпан, не обработано пачек {left} — повторите прогон за эту дату")
                break
            progress.subject(f"Ежедневный прогон, пачка XML {n}/{len(parts)}")
            if st_files.get("status") != "skipped":
                t = time.monotonic()
                try:
                    ids = await xa.pending_files(part)
                    scope["files"] += len(ids)
                    if ids:
                        if fa is None:
                            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

                            fa = AIFileAnalyzer(self.http_manager, self.environment, self.repo,
                                                push_results=self.push, run_cache=cache)
                        res = await fa.analyze_ids(ids)
                        _add_counts(st_files, self._counts(res))
                        file_results.update({int(r["file_id"]): slim_file(r["ai_analysis"]) for r in res
                                             if r.get("ai_analysis") and r.get("file_id") is not None})
                except Exception as e:  # noqa: BLE001 — сбой пачки не останавливает прогон
                    logger.error(f"Ежедневный прогон, файлы пачки {n}: {type(e).__name__}: {e}", exc_info=True)
                    st_files.update(status="failed", error=f"{type(e).__name__}: {e}"[:500])
                st_files["seconds"] += round(time.monotonic() - t)
            if st_xml.get("status") != "skipped":
                t = time.monotonic()
                try:
                    xa.file_results = file_results
                    res = await xa.analyze_ids(part)
                    _add_counts(st_xml, self._counts(res))
                    events.update({int(r["xml_id"]): slim_event(r["ai_analysis"]) for r in res if r.get("ai_analysis")})
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Ежедневный прогон, события пачки {n}: {type(e).__name__}: {e}", exc_info=True)
                    st_xml.update(status="failed", error=f"{type(e).__name__}: {e}"[:500])
                st_xml["seconds"] += round(time.monotonic() - t)
            cache.release_xml()
            logger.info(f"Ежедневный прогон: пачка {n}/{len(parts)} — файлы {st_files}, события {st_xml}; "
                        f"запросов к внешней БД {cache.queries}")
        self.stages["files"], self.stages["xml"] = st_files, st_xml

    async def _supplier_ids(self, found: List[Dict[str, Any]]) -> Any:
        """id организаций-поставщиков: ``POST /api/risk-monitoring/organisations`` (upsert по году, ИНН и КПП).

        Returns:
            Tuple[List[int], int]: id и число поставщиков, для которых запрос не удался.
        """
        api = self.__dict__.get("risk_api")
        if api is None:
            from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

            api = self.risk_api = RiskMonitoringAPI(self.http_manager, self.environment)
        ids, failed = [], 0
        for s in found:
            try:
                row = await api.upsert_organisation(s["inn"], s["kpp"], s["name"], short_name=s.get("short_name"))
                if row.get("id") is not None:
                    ids.append(int(row["id"]))
            except Exception as e:  # noqa: BLE001 — поставщик без строки организации пропускается
                failed += 1
                logger.warning(f"Организация-поставщик {s['inn']}/{s['kpp']} не получена: {type(e).__name__}: {e}")
        return list(dict.fromkeys(ids)), failed

    def _make_archives(self) -> Any:
        """Общий загрузчик архивов ЕИС прогона (удаляет скачанное в конце прогона)."""
        from configs.parsing import CloudStorageParser
        from risk_monitoring.xml_event_analyzer import ArchiveCache

        return ArchiveCache(CloudStorageParser(self.http_manager, None))

    async def _nearest(self, d: str, days: int = 7) -> List[Dict[str, Any]]:
        """Выгрузки ЕИС в окне ±``days`` дней (когда за дату XML нет: дата — это ``exact_date`` выгрузки, а не дата
        публикации документов)."""
        from datetime import timedelta

        day = parse_date(d)
        try:
            rows = await self._query("daily_dates_near.sql", head=[(day - timedelta(days=days)).isoformat(),
                                                                   (day + timedelta(days=days)).isoformat()])
        except Exception as e:  # noqa: BLE001 — только диагностика
            logger.warning(f"Выгрузки рядом с {d} не получены: {type(e).__name__}: {e}")
            return []
        return [{"date": str(r.get("exact_date"))[:10], "archives": int(_num(r.get("archives")) or 0),
                 "xml": int(_num(r.get("xml")) or 0)} for r in rows]

    async def _run(self, d: str, xml_rows: List[Dict[str, Any]], xml_ids: List[int], scope: Dict[str, Any],
                   file_results: Dict[int, Dict[str, Any]], cache: Any, started: str,
                   before: Dict[str, int]) -> Dict[str, Any]:
        """Этапы S1–S4 с общим кэшем прогона (см. :meth:`run`)."""
        from risk_monitoring.llm_json import CALLS
        from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

        xa = XmlEventAnalyzer(self.http_manager, self.environment, push_results=self.push, use_llm=self.use_llm,
                              results_repo=self.repo, run_cache=cache)

        events: Dict[int, Dict[str, Any]] = {}
        await self._documents(xml_ids, xa, cache, scope, file_results, events)
        events = {**await self._internal("xml", [i for i in xml_ids if i not in events]), **events}
        # закупка XML: привязка из внешней БД, иначе — найденная на этапе событий (ai_analysis.link) без нового запроса
        xml_cards: Dict[int, Optional[int]] = {}
        for r in xml_rows:
            card = r.get("card_id")
            if card is None:
                card = ((events.get(int(r["id"])) or {}).get("link") or {}).get("risk_monitoring_contract_id")
            xml_cards[int(r["id"])] = int(card) if card is not None else None
        card_ids = sorted({c for c in xml_cards.values() if c is not None})
        scope["contracts"] = len(card_ids)
        scope["xml_unlinked"] = sum(1 for c in xml_cards.values() if c is None)
        cards = {int(r["id"]): r for r in await self._query("daily_cards.sql", ids=card_ids)} if card_ids else {}
        changed: List[int] = []

        async def contracts() -> Any:
            """S2–S3a: затронутые закупки по уже проанализированным документам."""
            if not card_ids:
                return None, {"total": 0}
            from risk_monitoring.contract_analyzer import ContractAnalyzer

            ca = ContractAnalyzer(self.http_manager, self.environment, self.repo, push_results=self.push,
                                  use_llm=self.use_llm, analyze_documents=False)
            res = await ca.analyze_ids(card_ids)
            changed.extend(int(r["card_id"]) for r in res if r.get("status") == "done")
            return None, self._counts(res)

        await self._stage("contracts", contracts)
        contract_ai = await self._internal("contract", card_ids)
        monitored_orgs = sorted({int(c["organisation_id"]) for c in cards.values()
                                 if c.get("organisation_id") is not None and monitored(c)})

        async def customers() -> Any:
            """S3b: заказчики на мониторинге, чьи закупки затронуты."""
            if not monitored_orgs:
                return [], {"total": 0}
            from risk_monitoring.org_analyzer import OrgAnalyzer

            oa = OrgAnalyzer(self.http_manager, self.environment, self.repo, push_results=self.push,
                             use_llm=self.use_llm, roles=("customer",))
            res = await oa.analyze_ids(monitored_orgs)
            return res, self._counts(res)

        org_results = await self._stage("customers", customers) or []

        async def suppliers() -> Any:
            """S3c: поставщики затронутых закупок заказчиков на мониторинге — строка организации по API
            (существующая или новая, ``is_external = 1``), затем профиль поставщика (``OrgAnalyzer``)."""
            found = supplier_rows(cards, settings.DAILY_MONITORED_ONLY)
            if not found or not settings.DAILY_SUPPLIERS_ENABLED:
                return [], {"total": 0}
            ids, failed = await self._supplier_ids(found)
            if not ids:
                return [], {"total": len(found), "org_failed": failed}
            from risk_monitoring.org_analyzer import OrgAnalyzer

            oa = OrgAnalyzer(self.http_manager, self.environment, self.repo, push_results=self.push,
                             use_llm=self.use_llm, roles=("supplier",))
            res = await oa.analyze_ids(ids)
            return res, {**self._counts(res), "org_failed": failed}

        sup_results = await self._stage("suppliers", suppliers) or []
        summary = build_summary(export_date=d, environment=self.env, cards=cards, contracts=contract_ai,
                                changed=changed, events=events, xml_cards=xml_cards, customers=org_results,
                                suppliers=sup_results,
                                scope=scope, stages=self.stages)
        resume, used_llm = default_resume(summary), False
        if "summary" in self.stages_on and self.use_llm and settings.PACKAGE_LLM_RESUME:
            async with progress.step("S4: текст сводки (модель)"):
                resume, used_llm = await self._resume(summary)
        self.stages["summary"] = {"status": "completed", "llm": used_llm}
        summary["resume"] = resume
        summary["facts"] = summary_facts(summary)
        partial = any((v or {}).get("status") == "partial" for v in self.stages.values())
        summary["pipeline"] = {"schema_version": SCHEMA_VERSION, "stage": "S0-S4",
                               "status": "partial" if partial else "completed",
                               "started_at": started, "finished_at": passport.now_iso(),
                               "prompt_versions": {"daily_resume": PROMPT_VERSION} if used_llm else {},
                               "llm": {"requests": CALLS["requests"] - before.get("requests", 0),
                                       "failed": CALLS["failed"] - before.get("failed", 0)},
                               "external_queries": dict(cache.queries)}
        summary["pipeline"]["runs"] = self.__dict__.get("runs", []) + [
            {"started_at": started, "finished_at": summary["pipeline"]["finished_at"],
             "status": summary["pipeline"]["status"]}]
        summary["pipeline"]["attempt"] = len(summary["pipeline"]["runs"])
        await self.save(d, summary)
        logger.info(f"Ежедневный прогон {d}: {json.dumps(summary['scope'], ensure_ascii=False)}; "
                    f"этапы {json.dumps(self.stages, ensure_ascii=False, default=str)}")
        return summary

    async def _resume(self, summary: Dict[str, Any]) -> Any:
        """Текст сводки моделью по фактам; при сбое — шаблон."""
        try:
            from configs.llm_client import get_llm
            from risk_monitoring.llm_json import LLMJson, reasoning_llm

            client, model = reasoning_llm(*get_llm())
            text = await LLMJson(client, model, concurrency=1).ask_text(
                RESUME_SYSTEM, json.dumps({"facts": summary_facts(summary)}, ensure_ascii=False),
                max_tokens=1500, temperature=0.2)
            return (text, True) if text else (default_resume(summary), False)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Текст сводки шаблоном: {type(e).__name__}: {e}")
            return default_resume(summary), False


def previous_runs(prev: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Прежние запуски прогона за дату из сохранённой сводки (или отметки оборванного запуска).

    Args:
        prev: Сохранённый ``ai_analysis`` за дату.

    Returns:
        List[Dict[str, Any]]: ``started_at``, ``finished_at``, ``status`` по порядку; оборванный — ``status = aborted``.
    """
    pipe = (prev or {}).get("pipeline") or {}
    runs = [dict(r) for r in pipe.get("runs") or []]
    if pipe.get("started_at") and not any(r.get("started_at") == pipe["started_at"] for r in runs):
        runs.append({"started_at": pipe["started_at"], "finished_at": pipe.get("finished_at") or pipe.get("heartbeat_at"),
                     "status": "aborted" if pipe.get("status") == "running" else pipe.get("status")})
    return runs


def running_fresh(stored: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> bool:
    """Прогон за дату уже идёт: отметка «выполняется» с пульсом (``heartbeat_at``, иначе ``started_at``) моложе
    ``RUNNING_STALE_SECONDS``."""
    pipe = ((stored or {}).get("ai_analysis") or {}).get("pipeline") or {}
    if pipe.get("status") != "running":
        return False
    try:
        started = datetime.fromisoformat(str(pipe.get("heartbeat_at") or pipe.get("started_at")))
    except ValueError:
        return False
    now = now or (datetime.now(started.tzinfo) if started.tzinfo else datetime.now())
    return (now - started).total_seconds() < RUNNING_STALE_SECONDS


def completed(stored: Optional[Dict[str, Any]]) -> bool:
    """Сводка за дату уже готова."""
    return (((stored or {}).get("ai_analysis") or {}).get("pipeline") or {}).get("status") == "completed"

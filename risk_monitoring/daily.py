"""Ежедневный прогон риск-мониторинга за дату выгрузки ЕИС (S0–S4) и сводка.

Этапы (каждый сам пропускает уже проанализированное — повторный прогон за ту же дату дешёвый):

* **S0 охват** — XML ЕИС архивов с ``exact_date = D`` (``queries/rm/daily_xml.sql``);
* **S1b файлы** — ещё не проанализированные файлы этих XML (вложения, печатные формы) — ``AIFileAnalyzer``;
* **S1a события** — XML как события — ``XmlEventAnalyzer`` (с результатами файлов);
* **S2–S3a закупки** — затронутые закупки/контракты — ``ContractAnalyzer`` (документы уже проанализированы выше);
* **S3b заказчики** — заказчики затронутых закупок с ``monitoring = 1`` — ``OrgAnalyzer``;
* **S4 сводка** — по закупкам заказчиков на мониторинге: зоны, попавшие в красную зону, рост балла, важные события,
  реестр 19.28 и резюме (модель пересказывает готовые факты; без модели — шаблон).

Сводка хранится у нас (``ai_rm_results``, ``kind = daily``, ``external_id = ГГГГММДД``): таблицы сводок во внешней
БД пока нет. Отдаётся ``GET /risk-monitoring/daily/{date}``.

Сбой этапа не останавливает прогон: этап получает ``status = failed``, следующие работают с тем, что есть.
"""
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
PROMPT_VERSION = "daily-resume-1"
KIND = "daily"
STAGES = ("files", "xml", "contracts", "customers", "summary")
RED = "red"
# «Выполняется» дольше этого — прогон считается оборванным (пересборка, падение воркера), его можно запустить заново
RUNNING_STALE_SECONDS = 4 * 3600

RESUME_SYSTEM = (
    "Ты — аналитик риск-мониторинга госзакупок. Тебе дан список фактов ежедневной сводки за дату выгрузки ЕИС по "
    "заказчикам на мониторинге. Напиши сводку для руководителя: 4–6 предложений, по-русски, без вступлений — что "
    "пришло за сутки, что требует внимания в первую очередь (назови закупки и заказчиков из фактов), что проверить. "
    "Используй только эти факты: не добавляй цифр и выводов, которых в них нет. Если внимания ничего не требует — "
    "так и напиши. Ответ — обычный текст, без JSON и разметки.")


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


def monitored(card: Dict[str, Any]) -> bool:
    """Заказчик закупки на мониторинге (``monitoring = 1``; из MySQL приходит 1, 1.0, "1" или ``True``)."""
    try:
        return float(card.get("monitoring")) == 1
    except (TypeError, ValueError):
        return str(card.get("monitoring")).lower() == "true"


def _importance_rank(v: Optional[str]) -> int:
    """Ранг важности события (``low`` → 0 … ``critical`` → 3)."""
    from risk_monitoring.risk_catalog import IMPORTANCE_ORDER

    return IMPORTANCE_ORDER.index(v) if v in IMPORTANCE_ORDER else -1


def build_summary(*, export_date: str, environment: str, cards: Dict[int, Dict[str, Any]],
                  contracts: Dict[int, Dict[str, Any]], changed: Iterable[int], events: Dict[int, Dict[str, Any]],
                  xml_cards: Dict[int, Optional[int]], customers: Sequence[Dict[str, Any]], scope: Dict[str, Any],
                  stages: Dict[str, Any], cfg: Any = settings) -> Dict[str, Any]:
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
        add("became_red", i, f"Закупка перешла в красную зону: балл {_sc(i['score'])}"
                             + (f" (был {_sc(i['previous_score'])})" if i["previous_score"] is not None else " (первая оценка)"))
    for i in jumps:
        add("score_jump", i, f"Балл вырос на {_sc(i['delta'])} — до {_sc(i['score'])}")
    for e in important[:cfg.DAILY_TOP]:
        add("event", e, f"{e['title'] or e['code']}" + (f"; риски: {', '.join(e['risks'][:4])}" if e["risks"] else ""))

    cust = []
    for r in customers:
        sec = ((r.get("ai_analysis") or {}).get("customer")) or {}
        org = (r.get("ai_analysis") or {}).get("organisation") or {}
        if sec:
            cust.append({"organisation_id": r.get("organisation_id"), "canonical_id": r.get("canonical_id"),
                         "name": org.get("name"), "score": sec.get("score"), "zone": sec.get("zone"),
                         "delta": sec.get("delta"), "risks": [x.get("rule_id") for x in sec.get("risks") or []]})
    cust.sort(key=lambda c: -(c.get("score") or 0))
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
    if not sc.get("contracts_monitored") and not ev["total"]:
        facts.append("По заказчикам на мониторинге за эту дату новых документов нет.")
    else:
        z = c["by_zone"]
        facts.append(f"Закупки заказчиков на мониторинге по зонам риска: красная {z['red']}, жёлтая {z['yellow']}, "
                     f"зелёная {z['green']}; пересчитано в этом прогоне {c['changed']}.")
        if ev["total"]:
            types = "; ".join(f"{k} — {v}" for k, v in list(ev["by_type"].items())[:6])
            facts.append(f"Событий ЕИС по ним: {ev['total']} ({types}).")
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
            f"{x['name']} — балл {_sc(x['score'])} ({', '.join(x['risks'][:3])})" for x in risky[:5]) + ".")
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
    def _counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """Число результатов по статусам."""
        out: Dict[str, int] = {"total": len(results)}
        for r in results:
            out[r.get("status", "unknown")] = out.get(r.get("status", "unknown"), 0) + 1
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
        """Сохраняет сводку (или отметку «выполняется») у нас."""
        if hasattr(self.repo, "save_rm_result"):
            await self.repo.save_rm_result(self.env, KIND, run_key(export_date), ai, False, None)

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
        await self.save(d, {"date": d, "pipeline": {"schema_version": SCHEMA_VERSION, "stage": "S0-S4",
                                                    "status": "running", "started_at": started}})
        async with progress.step("S0: XML за дату выгрузки") as st:
            xml_rows = await self._query("daily_xml.sql", head=[d])
            xml_ids = [int(r["id"]) for r in xml_rows]
            st["detail"] = f"XML {len(xml_ids)}"
        scope: Dict[str, Any] = {"xml": len(xml_ids), "files": 0, "contracts": 0}
        file_results: Dict[int, Dict[str, Any]] = {}

        from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

        xa = XmlEventAnalyzer(self.http_manager, self.environment, push_results=self.push, use_llm=self.use_llm,
                              results_repo=self.repo)

        async def files() -> Any:
            """S1b: ещё не проанализированные файлы XML за дату."""
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

            ids = await xa.pending_files(xml_ids) if xml_ids else []
            scope["files"] = len(ids)
            if not ids:
                return None, {"total": 0}
            fa = AIFileAnalyzer(self.http_manager, self.environment, self.repo, push_results=self.push)
            res = await fa.analyze_ids(ids)
            file_results.update({int(r["file_id"]): r["ai_analysis"] for r in res
                                 if r.get("ai_analysis") and r.get("file_id") is not None})
            return None, self._counts(res)

        async def xml() -> Any:
            """S1a: события XML."""
            if not xml_ids:
                return {}, {"total": 0}
            xa.file_results = file_results
            res = await xa.analyze_ids(xml_ids)
            return {int(r["xml_id"]): r["ai_analysis"] for r in res if r.get("ai_analysis")}, self._counts(res)

        await self._stage("files", files)
        events = await self._stage("xml", xml) or {}
        # привязка XML к закупкам могла измениться на этапе событий — охват закупок перечитывается
        xml_rows = await self._query("daily_xml.sql", head=[d]) if xml_ids else []
        xml_cards = {int(r["id"]): (int(r["card_id"]) if r.get("card_id") is not None else None) for r in xml_rows}
        events = {**await self._internal("xml", [i for i in xml_ids if i not in events]), **events}
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
        summary = build_summary(export_date=d, environment=self.env, cards=cards, contracts=contract_ai,
                                changed=changed, events=events, xml_cards=xml_cards, customers=org_results,
                                scope=scope, stages=self.stages)
        resume, used_llm = default_resume(summary), False
        if "summary" in self.stages_on and self.use_llm and settings.PACKAGE_LLM_RESUME:
            async with progress.step("S4: текст сводки (модель)"):
                resume, used_llm = await self._resume(summary)
        self.stages["summary"] = {"status": "completed", "llm": used_llm}
        summary["resume"] = resume
        summary["facts"] = summary_facts(summary)
        summary["pipeline"] = {"schema_version": SCHEMA_VERSION, "stage": "S0-S4", "status": "completed",
                               "started_at": started, "finished_at": passport.now_iso(),
                               "prompt_versions": {"daily_resume": PROMPT_VERSION} if used_llm else {},
                               "llm": {"requests": CALLS["requests"] - before.get("requests", 0),
                                       "failed": CALLS["failed"] - before.get("failed", 0)}}
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


def running_fresh(stored: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> bool:
    """Прогон за дату уже идёт (отметка «выполняется» моложе ``RUNNING_STALE_SECONDS``)."""
    pipe = ((stored or {}).get("ai_analysis") or {}).get("pipeline") or {}
    if pipe.get("status") != "running":
        return False
    try:
        started = datetime.fromisoformat(str(pipe.get("started_at")))
    except ValueError:
        return False
    now = now or (datetime.now(started.tzinfo) if started.tzinfo else datetime.now())
    return (now - started).total_seconds() < RUNNING_STALE_SECONDS


def completed(stored: Optional[Dict[str, Any]]) -> bool:
    """Сводка за дату уже готова."""
    return (((stored or {}).get("ai_analysis") or {}).get("pipeline") or {}).get("status") == "completed"

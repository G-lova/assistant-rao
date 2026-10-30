"""Аналитика организаций как заказчика и поставщика (этапы S3b–S3c) по id ``risk_monitoring_organisations``.

Организация бывает в обеих ролях, поэтому ``ai_analysis`` делится на секции ``customer`` и ``supplier``
(``org_profile``). Данные — закупки за ``settings.ORG_PERIOD_MONTHS`` месяцев (``dataset`` и уже рассчитанный
профиль закупок ``contracts.ai_analysis.profile``), реестр 19.28. Модель пишет только короткое резюме секции.

Куда пишется результат: каноническая строка группы (``xml_events.canonical_org``). Раньше внешняя система не
принимала ``ai_analysis`` организаций; по ТЗ 09.10.2026 колонка ``risk_monitoring_organisations.ai_analysis`` есть,
запись включена (``settings.ORG_PUSH_ENABLED``). Результат всегда сохраняется и у нас (``ai_rm_results``,
``kind = organisation``) и возвращается в задаче.
"""
import asyncio
import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from configs.config import Config
from configs.logger import get_logger
from configs import settings
from risk_monitoring import control_checks, org_profile, passport, progress, rm_sql, text_format
from risk_monitoring.contract_analyzer import _none
from risk_monitoring.xml_events import canonical_org

logger = get_logger(__name__)

EXTERNAL_TABLE = "risk_monitoring_organisations"
PROMPT_VERSION = "org-resume-6"
ROLES = ("customer", "supplier")

RESUME_SYSTEM = (
    "Ты — аналитик риск-мониторинга госзакупок. Тебе дан список фактов об организации в роли {role} за период. "
    "Перескажи их резюме: до 6 предложений, по-русски, без вступлений — объём закупок, признаки риска "
    "(если есть), что проверить. Используй только эти факты: не добавляй цифр, сравнений и выводов, которых в них "
    "нет, не пересчитывай доли. Если признаков риска нет или данных мало — так и напиши, не ищи риски сам. "
    "Не используй коды рисков и правил (вида DOC-002, FIN-004, BEH-005, pz_p1) — называй риски словами, понятными заказчику. " + text_format.instruction())
ROLE_NAMES = {"customer": "заказчика", "supplier": "поставщика"}


def _key_customer(r: Dict[str, Any]) -> Tuple[str, str]:
    """Ключ заказчика: ИНН и КПП."""
    return (str(r.get("inn") or "").strip(), str(r.get("kpp") or "").strip())


class OrgAnalyzer:
    """Аналитика организаций.

    Args:
        http_manager: ``HTTPClientManager``.
        environment: Окружение внешней системы.
        repo: ``AIRepo`` (хранилище результатов и прошлых баллов).
        push_results: Записывать во внешнюю БД (только при ``settings.ORG_PUSH_ENABLED``).
        use_llm: Резюме пишет модель.
        roles: Какие секции считать (``customer``, ``supplier``).
        period_months: Период, месяцев (по умолчанию ``settings.ORG_PERIOD_MONTHS``).
    """

    def __init__(self, http_manager: Any, environment: str, repo: Any = None, push_results: bool = True,
                 use_llm: bool = True, roles: Tuple[str, ...] = ROLES, period_months: Optional[int] = None):
        """Создаёт доступ к внешней БД, клиент записи и модель для резюме."""
        from configs.data_fetcher import DataFetcher
        from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

        cfg = Config()
        db = cfg.get_database_config(environment)
        self.env = (environment or cfg.APP_ENV).lower()
        self.repo = repo
        self.push_results = push_results and settings.ORG_PUSH_ENABLED
        self.roles = tuple(r for r in roles if r in ROLES) or ROLES
        self.period_months = int(period_months or settings.ORG_PERIOD_MONTHS)
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)
        self.risk_api = RiskMonitoringAPI(http_manager, environment)
        self.llm, self.model = None, None
        if use_llm and settings.PACKAGE_LLM_RESUME:
            from configs.llm_client import get_llm
            from risk_monitoring.llm_json import LLMJson, reasoning_llm

            client, self.model = reasoning_llm(*get_llm())
            self.llm = LLMJson(client, self.model, concurrency=2)
        self.stats: Dict[str, Any] = {"llm_resume": 0}

    async def _query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Выполняет ``queries/rm/<name>``."""
        sql, bindings = rm_sql.render(name, head=head, **lists)
        df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [{k: _none(v) for k, v in r.items()} for r in df.to_dict("records")] if not df.empty else []

    async def _safe_query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Запрос, сбой которого не останавливает анализ."""
        try:
            return await self._query(name, head, **lists)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Запрос {name} не выполнен: {type(e).__name__}: {e}")
            return []

    def since(self, today: Optional[date] = None) -> str:
        """Начало периода аналитики (``ГГГГ-ММ-ДД``)."""
        return ((today or date.today()) - timedelta(days=round(30.44 * self.period_months))).isoformat()

    # ------------------------------------------------------------------ вход
    async def analyze_ids(self, org_ids: List[int]) -> List[Dict[str, Any]]:
        """Аналитика организаций по id.

        Args:
            org_ids: id ``risk_monitoring_organisations`` (любая строка группы — по году или дубль).

        Returns:
            List[Dict[str, Any]]: ``organisation_id``, ``canonical_id``, ``status``, баллы секций, ``ai_analysis``.
        """
        ids = list(dict.fromkeys(int(i) for i in org_ids))
        progress.subject(f"Организации ({len(ids)})")
        async with progress.step("чтение организаций и их строк по годам"):
            rows = {int(r["id"]): r for r in await self._query("org_rows.sql", ids=ids)}
        inns = sorted({str(r.get("inn")).strip() for r in rows.values() if r.get("inn")})
        group = await self._query("org_group.sql", inns=inns) if inns else []
        year = date.today().year
        targets: Dict[int, Dict[str, Any]] = {}
        result_for: Dict[int, int] = {}
        for oid, r in rows.items():
            inn = str(r.get("inn") or "").strip()
            if not inn:
                continue
            cust_rows = [g for g in group if _key_customer(g) == _key_customer(r)]
            sup_rows = [g for g in group if str(g.get("inn") or "").strip() == inn]
            canon = canonical_org(cust_rows or [r], year) or r
            targets.setdefault(int(canon["id"]), {"row": canon, "customer_rows": cust_rows or [r], "inn": inn,
                                                  "supplier_rows": sup_rows or [r]})
            result_for[oid] = int(canon["id"])
        since = self.since()
        async with progress.step(f"чтение закупок заказчиков и контрактов поставщиков с {since}") as st:
            cust_ids = sorted({int(g["id"]) for t in targets.values() for g in t["customer_rows"]})
            customer_contracts = await self._query("customer_contracts.sql", head=[since], ids=cust_ids) \
                if "customer" in self.roles and cust_ids else []
            sup_inns = sorted({t["inn"] for t in targets.values()})
            supplier_contracts = await self._query("supplier_contracts.sql", head=[since], inns=sup_inns) \
                if "supplier" in self.roles and sup_inns else []
            black_inns = sorted({str(c.get("supplier_inn")).strip() for c in customer_contracts if c.get("supplier_inn")} | set(sup_inns))
            black = await self._safe_query("black_list.sql", inns=black_inns) if black_inns else []
            rnp = await self._safe_query("rnp_entries.sql", inns=sorted(sup_inns)) if sup_inns and "supplier" in self.roles else []
            # жалобы: заявитель раскрывается только в жалобе — по ИНН заказчиков и поставщиков
            org_inns = sorted({t["inn"] for t in targets.values()})
            nums = sorted({str(c["number"]) for c in customer_contracts if c.get("number")})
            rows_c = await self._safe_query("complaints_by_inn.sql", head=[since], inns=org_inns, inns2=org_inns,
                                            nums=nums) if org_inns else []
            # отзыв, решение и приостановка той же жалобы — без ИНН заявителя, по номеру жалобы
            numbers = sorted({str(r["complaint_number"]) for r in rows_c if r.get("complaint_number")})
            more = await self._safe_query("complaints_by_number.sql", nums=numbers) if numbers else []
            self.complaint_rows = rows_c + more
            check_since = (date.today() - timedelta(days=settings.CHECK_LOOKBACK_DAYS)).isoformat()
            self.checks = control_checks.normalize(await self._safe_query("checks_recent.sql", [check_since])) \
                if "customer" in self.roles else []
            stored = await self._internal(list(targets))
            st["detail"] = f"как заказчик {len(customer_contracts)}, как поставщик {len(supplier_contracts)}, реестр {len(black)}"

        done: Dict[int, Dict[str, Any]] = {}
        sem = asyncio.Semaphore(3)

        async def one(cid: int, t: Dict[str, Any]) -> None:
            """Одна организация с обработкой ошибок."""
            async with sem:
                try:
                    done[cid] = await self._analyze(cid, t, customer_contracts, supplier_contracts, black, rnp,
                                                    stored.get(cid), since)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Ошибка аналитики организации {cid}: {e}", exc_info=True)
                    done[cid] = {"status": "error", "error": f"{type(e).__name__}: {e}", "pushed": False}

        await asyncio.gather(*(one(cid, t) for cid, t in targets.items()))
        out = []
        for oid in ids:
            if oid not in rows:
                out.append({"organisation_id": oid, "status": "not_found"})
            elif oid not in result_for:
                out.append({"organisation_id": oid, "status": "skipped", "reason": "no_inn"})
            else:
                cid = result_for[oid]
                out.append({"organisation_id": oid, "canonical_id": cid, **done[cid],
                            **({"duplicate_of": cid} if oid != cid and cid in ids else {})})
        return out

    async def _analyze(self, cid: int, t: Dict[str, Any], customer_contracts: List[Dict[str, Any]],
                       supplier_contracts: List[Dict[str, Any]], black: List[Dict[str, Any]], rnp: List[Dict[str, Any]],
                       stored: Optional[Dict[str, Any]], since: str) -> Dict[str, Any]:
        """Секции одной организации, резюме, сохранение и запись."""
        prev = (stored or {}).get("ai_analysis") or {}
        name = t["row"].get("short_name") or t["row"].get("name")
        progress.subject(f"Организация {cid} «{name or ''}»")
        ai: Dict[str, Any] = {"organisation": {"canonical_id": cid, "inn": t["inn"], "kpp": t["row"].get("kpp"),
                                               "name": name, "rows": sorted(int(g["id"]) for g in t["customer_rows"])},
                              "period": {"since": since, "months": self.period_months}}
        summary: Dict[str, Any] = {}
        if "customer" in self.roles:
            ids = {int(g["id"]) for g in t["customer_rows"]}
            mine = [c for c in customer_contracts if c.get("organisation_id") is not None and int(c["organisation_id"]) in ids]
            if mine:
                sec = org_profile.customer_section(mine, black, settings, org_profile.previous_customer_score(prev),
                                                   complaint_items=org_profile.complaints(
                                                       getattr(self, "complaint_rows", []), t["inn"], "customer",
                                                       [c.get("number") for c in mine]),
                                                   check_metrics=control_checks.customer_metrics(
                                                       getattr(self, "checks", []), t["inn"],
                                                       [c.get("number") for c in mine]))
                progress.note(f"заказчик: закупок {len(mine)}, рисков {len(sec['risks'])}, балл {sec['score']}")
                async with progress.step("резюме заказчика"):
                    sec["resume"] = await self._resume("customer", name, sec)
                ai["customer"] = sec
                summary["customer_score"] = sec["score"]
        if "supplier" in self.roles:
            mine = [c for c in supplier_contracts if str(c.get("supplier_inn") or "").strip() == t["inn"]]
            sup_black = [b for b in black if str(b.get("inn") or "").strip() == t["inn"]]
            sup_rnp = [e for e in rnp if str(e.get("inn") or "").strip() == t["inn"]]
            filed = org_profile.complaints(getattr(self, "complaint_rows", []), t["inn"], "supplier")
            if mine or sup_black or sup_rnp or filed:
                sec = org_profile.supplier_section(mine, sup_black, t["inn"], settings,
                                                   (prev.get("supplier") or {}).get("score"), rnp=sup_rnp,
                                                   complaint_items=filed)
                progress.note(f"поставщик: контрактов {len(mine)}, рисков {len(sec['risks'])}, балл {sec['score']}")
                async with progress.step("резюме поставщика"):
                    sec["resume"] = await self._resume("supplier", name, sec)
                ai["supplier"] = sec
                summary["supplier_score"] = sec["score"]
        pipeline = passport.pipeline_block("S3", "completed", self.model if self.llm else None,
                                           {"org_resume": PROMPT_VERSION} if self.llm else {})
        pipeline["schema_version"] = org_profile.SCHEMA_VERSION
        ai["pipeline"] = pipeline
        result = {"status": "done" if (ai.get("customer") or ai.get("supplier")) else "no_data", "pushed": False,
                  **summary, "ai_analysis": ai}
        if self.push_results and result["status"] == "done":
            try:
                async with progress.step("запись результата во внешнюю БД"):
                    await self._push(cid, ai)
                result["pushed"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"Не удалось записать ai_analysis организации {cid}: {e}")
                result["push_error"] = str(e)
        if self.repo is not None and hasattr(self.repo, "save_rm_result"):
            try:
                async with progress.step("сохранение результата во внутренней БД"):
                    await self.repo.save_rm_result(self.env, "organisation", cid, ai, result["pushed"], result.get("push_error"))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Результат организации {cid} не сохранён: {type(e).__name__}: {e}")
        return result

    async def _push(self, cid: int, ai: Dict[str, Any]) -> None:
        """Запись «прочитать → слить секции → записать»: секция другой роли, посчитанная раньше, не теряется."""
        from risk_monitoring import risk_monitoring_api

        risk_monitoring_api.ALLOWED_TABLES.add(EXTERNAL_TABLE)
        current = {}
        try:
            rec = await self.risk_api.get_record(EXTERNAL_TABLE, cid)
            current = rec.get("ai_analysis") or {}
            current = json.loads(current) if isinstance(current, str) else current
        except Exception as e:  # noqa: BLE001 — нет записи или поля: пишем как есть
            logger.info(f"ai_analysis организации {cid} не прочитан ({type(e).__name__}): пишем без слияния")
        await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, cid, {**(current or {}), **ai})

    async def _internal(self, ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Прошлые результаты организаций у нас."""
        if not ids or self.repo is None or not hasattr(self.repo, "get_rm_results"):
            return {}
        try:
            return await self.repo.get_rm_results(self.env, "organisation", ids)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Прошлые результаты организаций не получены: {type(e).__name__}: {e}")
            return {}

    async def _resume(self, role: str, name: str, sec: Dict[str, Any]) -> str:
        """Резюме секции моделью или шаблоном."""
        fallback = org_profile.default_resume(role, name, sec, settings)
        if self.llm is None:
            return fallback
        payload = {"organisation": name, "facts": org_profile.resume_facts(role, sec, settings)}
        try:
            text = await self.llm.ask_text(RESUME_SYSTEM.format(role=ROLE_NAMES[role]),
                                           json.dumps(payload, ensure_ascii=False, default=str),
                                           max_tokens=1200, temperature=0.2)
            self.stats["llm_resume"] += 1
            return text_format.normalize(text or fallback)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Резюме организации {name} шаблоном: {type(e).__name__}: {e}")
            return fallback

"""Анализ закупки/контракта по карточке ``risk_monitoring_contracts`` (этапы S2–S3a) и запись ``ai_analysis``.

Документы повторно не анализируются: берутся сохранённые результаты файлов и XML — во внешней БД, а если их там нет,
у нас (``ai_rm_results``). Файлы и XML закупки, у которых результата нет ни там, ни там, анализируются
(``analyze_documents``): сначала файлы, затем XML (событие получает резюме и риски файлов). Затем — значения правил,
реестр 19.28 и ``dataset`` (правила сборки — ``contract_package``).
Модель пишет только резюме (``settings.PACKAGE_LLM_RESUME``), один короткий запрос на закупку.

Закупка — сводный объект, его анализ пересчитывается при каждом запросе. Если входные данные не изменились
(``pipeline.input_hash``), пересчёт пропускается (``status = unchanged``), кроме режима отладки (``force``).
"""
import asyncio
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from configs.config import Config
from configs.logger import get_logger
from risk_monitoring import contract_package as cp
from configs import settings
from risk_monitoring import passport, progress, rm_sql

logger = get_logger(__name__)

EXTERNAL_TABLE = "risk_monitoring_contracts"
STAGE = "S2"
PROMPT_VERSION = "contract-resume-2"

RESUME_SYSTEM = (
    "Ты — аналитик риск-мониторинга госзакупок (44-ФЗ, 223-ФЗ). По паспорту закупки, её рискам и событиям напиши "
    "резюме для аналитика: 4–7 предложений, по-русски, без вступлений. Что закупают и на какой стадии, главные риски "
    "со ссылкой на источник (документ, событие ЕИС, правило, реестр), что проверить в первую очередь. Не придумывай "
    "фактов, которых нет во входных данных; не делай вывод о нарушении — только о признаках риска. Не используй коды рисков и правил (вида DOC-002, FIN-004, BEH-005, pz_p1) — называй риски словами, понятными заказчику. Ответ — "
    "обычный текст, без JSON, списков и разметки.")
RESUME_SCHEMA = {"type": "object", "properties": {"resume": {"type": "string"}}, "required": ["resume"]}


def _none(v: Any) -> Any:
    """NaN/NaT из DataFrame и JSON ``null`` → ``None``.

    ``JSON_UNQUOTE(JSON_EXTRACT(...))`` над значением ``null`` возвращает строку ``"null"``: без замены она
    считалась признаком изменения/расторжения контракта и «поставщиком» с ИНН ``null`` (аналитика организаций
    20.10.2026: изменено и расторгнуто 60 % контрактов у всех трёх заказчиков, HHI по поставщику ``null``).
    """
    if isinstance(v, str) and v.strip().lower() == "null":
        return None
    try:
        import pandas as pd

        return None if (not isinstance(v, (list, dict, str, bytes)) and pd.isna(v)) else v
    except (TypeError, ValueError, ImportError):
        return v


def _counts(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Счётчики результатов анализа документов по статусам."""
    out: Dict[str, int] = {}
    for r in results:
        out[r.get("status", "unknown")] = out.get(r.get("status", "unknown"), 0) + 1
    return out


class ContractAnalyzer:
    """Анализ закупок/контрактов по id карточек.

    Args:
        http_manager: ``HTTPClientManager``.
        environment: Окружение внешней системы (``X-API-Database``).
        repo: ``AIRepo`` — внутреннее хранилище результатов (``ai_rm_results``); ``None`` — без него.
        push_results: Записывать ``ai_analysis`` во внешнюю БД.
        force: Пересчитать, даже если входные данные не изменились (режим отладки).
        use_llm: Резюме пишет модель (иначе — шаблон); также ``EventAnalyzer`` для непроанализированных XML.
        analyze_documents: Анализировать файлы и XML закупки, у которых нет результата ни во внешней БД, ни у нас.
    """

    def __init__(self, http_manager: Any, environment: str, repo: Any = None, push_results: bool = True,
                 force: bool = False, use_llm: bool = True, analyze_documents: bool = True):
        """Создаёт доступ к внешней БД, клиент записи и (при ``use_llm``) модель для резюме."""
        from configs.data_fetcher import DataFetcher
        from risk_monitoring.risk_monitoring_api import RiskMonitoringAPI

        cfg = Config()
        db = cfg.get_database_config(environment)
        self.env = (environment or cfg.APP_ENV).lower()
        self.repo = repo
        self.http_manager = http_manager
        self.environment = environment
        self.use_llm = use_llm
        self.analyze_documents = analyze_documents
        self.push_results = push_results
        self.force = force
        self.data_fetcher = DataFetcher(db.url, db.headers, http_manager)
        self.risk_api = RiskMonitoringAPI(http_manager, environment)
        self.llm = None
        self.model = None
        if use_llm and settings.PACKAGE_LLM_RESUME:
            from configs.llm_client import get_llm
            from risk_monitoring.llm_json import LLMJson, reasoning_llm

            client, self.model = reasoning_llm(*get_llm())
            self.llm = LLMJson(client, self.model, concurrency=2)
        self.stats: Dict[str, Any] = {"llm_resume": 0}
        self.documents: Dict[str, Any] = {}

    async def _query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Выполняет ``queries/rm/<name>`` и возвращает строки как словари."""
        sql, bindings = rm_sql.render(name, head=head, **lists)
        df = await self.data_fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [{k: _none(v) for k, v in r.items()} for r in df.to_dict("records")] if not df.empty else []

    async def _safe_query(self, name: str, head: List[Any] = (), **lists: List[Any]) -> List[Dict[str, Any]]:
        """Запрос, сбой которого не останавливает анализ (таблицы может не быть в окружении)."""
        try:
            return await self._query(name, head, **lists)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Запрос {name} не выполнен: {type(e).__name__}: {e}")
            return []

    async def _internal(self, kind: str, ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Сохранённые у нас результаты (``ai_rm_results``)."""
        if not ids or self.repo is None or not hasattr(self.repo, "get_rm_results"):
            return {}
        try:
            return await self.repo.get_rm_results(self.env, kind, ids)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Внутренние результаты ({kind}) не получены: {type(e).__name__}: {e}")
            return {}

    # ------------------------------------------------------------------ сбор входных данных
    async def _fill_from_internal(self, files: List[Dict[str, Any]]) -> None:
        """Файлы без результата во внешней БД получают тип, резюме, риски и суммы из нашего хранилища."""
        missing = [int(f["id"]) for f in files if f.get("rm_status") != "completed"]
        stored = await self._internal("file", missing)
        for f in files:
            s = stored.get(int(f["id"]))
            if not s or s.get("status") != "completed":
                continue
            ai = s["ai_analysis"] or {}
            f.update(rm_status="completed", source="internal", detected_type=(ai.get("doc_type") or {}).get("detected_type"),
                     resume=ai.get("resume"), total_doc_risk=ai.get("total_doc_risk"), risks=ai.get("risks"),
                     finances=(ai.get("raw_data") or {}).get("finances") if isinstance(ai.get("raw_data"), dict) else None,
                     superseded_by=ai.get("superseded_by"), content_sha256=ai.get("content_sha256"))

    async def _texts(self, files: List[Dict[str, Any]]) -> Dict[int, str]:
        """Тексты действующих документов закупки из нашего хранилища (``ai_documents`` по ``content_sha256``).

        Документ без хэша, с удалённым политикой хранения текстом или при сбое БД пропускается.

        Returns:
            Dict[int, str]: ``file_id → текст``.
        """
        if self.repo is None or not hasattr(self.repo, "get_document_by_sha"):
            return {}
        out: Dict[int, str] = {}
        for f in files:
            sha = f.get("content_sha256")
            if f.get("superseded_by") or not sha or sha == "null":
                continue
            try:
                row = await self.repo.get_document_by_sha(str(sha))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Текст файла {f.get('id')} не получен: {type(e).__name__}: {e}")
                continue
            if row and row["text"]:
                out[int(f["id"])] = row["text"]
        return out

    async def _fill_xml_from_internal(self, xmls: List[Dict[str, Any]]) -> None:
        """XML без результата во внешней БД получают событие, резюме и риски из нашего хранилища."""
        missing = [int(x["id"]) for x in xmls if x.get("rm_status") != "completed"]
        stored = await self._internal("xml", missing)
        for x in xmls:
            s = stored.get(int(x["id"]))
            if s and s.get("status") == "completed":
                ai = s["ai_analysis"] or {}
                x.update(rm_status="completed", source="internal", event=ai.get("event"), resume=ai.get("resume"),
                         risks=ai.get("risks"))

    async def _analyze_missing(self, data: Dict[str, Any]) -> bool:
        """Анализирует файлы и XML закупок, у которых нет результата ни во внешней БД, ни у нас.

        Файлы — ``AIFileAnalyzer`` (уже проанализированные и исчерпавшие попытки он сам пропускает), затем XML —
        ``XmlEventAnalyzer`` со свежими результатами файлов. Результаты пишутся во внешнюю БД (``push``) и у нас.

        Returns:
            bool: Что-то проанализировано — входные данные нужно перечитать.
        """
        file_ids = [int(f["id"]) for f in data["files"] if f.get("rm_status") != "completed"]
        xml_ids = [int(x["id"]) for x in data["xmls"] if x.get("rm_status") != "completed"]
        if not (file_ids or xml_ids) or self.repo is None:
            progress.note("все файлы и XML закупок уже проанализированы")
            return False
        logger.info(f"Закупки: анализ недостающих документов — файлов {len(file_ids)}, XML {len(xml_ids)}")
        fresh: Dict[int, Dict[str, Any]] = {}
        if file_ids:
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

            fa = AIFileAnalyzer(self.http_manager, self.environment, self.repo, push_results=self.push_results)
            f_res = await fa.analyze_ids(file_ids)
            fresh = {int(r["file_id"]): r["ai_analysis"] for r in f_res if r.get("ai_analysis") and r.get("file_id") is not None}
            self.documents["files"] = _counts(f_res)
            self.documents["files_stats"] = fa.stats
        if xml_ids:
            from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

            xa = XmlEventAnalyzer(self.http_manager, self.environment, push_results=self.push_results,
                                  use_llm=self.use_llm, results_repo=self.repo)
            xa.file_results = fresh
            self.documents["xml"] = _counts(await xa.analyze_ids(xml_ids))
            self.documents["xml_stats"] = getattr(xa, "stats", {})
        return True

    async def _load(self, ids: List[int]) -> Dict[str, Any]:
        """Все входные данные по карточкам одним набором запросов."""
        cards = await self._query("contract_cards.sql", ids=ids)
        files = await self._query("contract_files.sql", ids=ids)
        await self._fill_from_internal(files)
        xmls = await self._query("contract_xml.sql", ids=ids)
        await self._fill_xml_from_internal(xmls)
        rules = await self._safe_query("contract_rules.sql", ids=ids)
        statuses_rows = await self._safe_query("file_risk_statuses.sql", ids=[int(f["id"]) for f in files]) if files else []
        inns = sorted({str(i) for i in ((cp.as_json(c.get("dataset")) or {}).get("supplier", {}).get("post_inn") for c in cards) if i})
        black = await self._safe_query("black_list.sql", inns=inns) if inns else []
        rnp = await self._safe_query("rnp_entries.sql", inns=inns) if inns else []
        supplier_counts = await self._supplier_counts(cards)
        return {"cards": cards, "files": files, "xmls": xmls, "rules": rules, "statuses": statuses_rows, "black": black, "rnp": rnp,
                "supplier_counts": supplier_counts}

    async def _supplier_counts(self, cards: List[Dict[str, Any]]) -> Dict[int, Dict[str, int]]:
        """Контракты заказчика каждой закупки за период по поставщикам (для BEH-006 «любимый поставщик»).

        Заказчик — все строки ``risk_monitoring_organisations`` с его ИНН/КПП (по годам и дубли).

        Returns:
            Dict[int, Dict[str, int]]: ``id карточки → {ИНН поставщика: число контрактов}``.
        """
        from datetime import date, timedelta

        org_ids = sorted({int(c["organisation_id"]) for c in cards if c.get("organisation_id") is not None})
        if not org_ids:
            return {}
        rows = await self._safe_query("org_rows.sql", ids=org_ids)
        inns = sorted({str(r["inn"]).strip() for r in rows if r.get("inn")})
        group = await self._safe_query("org_group.sql", inns=inns) if inns else []
        key = {int(r["id"]): (str(r.get("inn") or "").strip(), str(r.get("kpp") or "").strip()) for r in rows + group}
        all_ids = sorted({i for i, k in key.items() if k in {key.get(o) for o in org_ids}})
        since = (date.today() - timedelta(days=round(30.44 * settings.ORG_PERIOD_MONTHS))).isoformat()
        counts_rows = await self._safe_query("customer_supplier_counts.sql", head=[since], ids=all_ids) if all_ids else []
        by_key: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in counts_rows:
            k = key.get(int(r["organisation_id"])) if r.get("organisation_id") is not None else None
            inn = str(r.get("supplier_inn") or "").strip()
            if k and inn:
                by_key[k][inn] += int(cp.num(r.get("contracts")) or 0)
        return {int(c["id"]): dict(by_key.get(key.get(int(c["organisation_id"])), {}))
                for c in cards if c.get("organisation_id") is not None}

    @staticmethod
    def _group(data: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
        """Раскладывает входные данные по карточкам."""
        by: Dict[int, Dict[str, Any]] = {int(c["id"]): {"card": c, "files": [], "xmls": [], "values": None}
                                         for c in data["cards"]}
        for f in data["files"]:
            if f.get("card_id") is not None and int(f["card_id"]) in by:
                by[int(f["card_id"])]["files"].append(f)
        for x in data["xmls"]:
            if x.get("card_id") is not None and int(x["card_id"]) in by:
                by[int(x["card_id"])]["xmls"].append(x)
        for r in data["rules"]:  # отсортированы по убыванию даты расчёта — берём первый
            k = int(r["card_id"])
            if k in by and by[k]["values"] is None:
                by[k]["values"] = cp.as_json(r.get("rule_values")) or {}
        return by

    # ------------------------------------------------------------------ вход
    async def analyze_ids(self, card_ids: List[int]) -> List[Dict[str, Any]]:
        """Анализирует закупки по id карточек.

        Args:
            card_ids: id ``risk_monitoring_contracts``.

        Returns:
            List[Dict[str, Any]]: По одному результату на id: ``status`` (``done`` / ``unchanged`` / ``not_found`` /
            ``error``), ``pushed``, ``ai_analysis``.
        """
        ids = list(dict.fromkeys(int(i) for i in card_ids))
        progress.subject(f"Закупки ({len(ids)})")
        async with progress.step("чтение закупок, файлов, XML, правил и реестра 19.28") as st:
            data = await self._load(ids)
            st["detail"] = (f"закупок {len(data['cards'])}, файлов {len(data['files'])}, XML {len(data['xmls'])}, "
                            f"расчётов правил {len(data['rules'])}")
        if self.analyze_documents and await self._analyze_missing(data):
            progress.subject(f"Закупки ({len(ids)})")
            async with progress.step("повторное чтение данных закупок после анализа документов"):
                data = await self._load(ids)   # результаты документов — во внешней БД или у нас
        by = self._group(data)
        statuses: Dict[Tuple[int, str], int] = {}
        for s in data["statuses"]:
            try:
                statuses[(int(s["file_id"]), s["rule_id"])] = int(s["status"])
            except (TypeError, ValueError):
                continue
        black_by_inn: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for b in data["black"]:
            black_by_inn[str(b.get("inn") or "").strip()].append(b)
        self.rnp_by_inn: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for e in data.get("rnp") or []:
            self.rnp_by_inn[str(e.get("inn") or "").strip()].append(e)
        sem = asyncio.Semaphore(3)

        async def one(cid: int) -> Dict[str, Any]:
            """Одна карточка с обработкой ошибок."""
            async with sem:
                try:
                    return await self._analyze_card(by[cid], statuses, black_by_inn,
                                                    data.get("supplier_counts", {}).get(cid))
                except Exception as e:  # noqa: BLE001 — сбой одной закупки не останавливает остальные
                    logger.error(f"Ошибка анализа закупки {cid}: {e}", exc_info=True)
                    return {"card_id": cid, "status": "error", "error": f"{type(e).__name__}: {e}", "pushed": False}

        results = await asyncio.gather(*(one(i) for i in ids if i in by))
        return list(results) + [{"card_id": i, "status": "not_found"} for i in ids if i not in by]

    async def _analyze_card(self, item: Dict[str, Any], statuses: Dict[Tuple[int, str], int],
                            black_by_inn: Dict[str, List[Dict[str, Any]]],
                            supplier_counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Анализ одной закупки: сборка, резюме, запись."""
        card = item["card"]
        cid = int(card["id"])
        progress.subject(f"Закупка {cid} ({card.get('number') or ''})")
        dataset = cp.as_json(card.get("dataset")) or {}
        inn = str((dataset.get("supplier") or {}).get("post_inn") or "").strip()
        if cp.absence_claims(item["files"]):
            async with progress.step("проверка «отсутствует …» по текстам документов закупки") as st:
                texts = await self._texts(item["files"])
                n = cp.drop_absence_contradicted(item["files"], texts, statuses)
                st["detail"] = f"текстов {len(texts)}, снято рисков {n}"
        built = cp.build_contract_analysis(card=card, files=item["files"], xmls=item["xmls"], values=item["values"],
                                           black=black_by_inn.get(inn, []), statuses=statuses,
                                           previous_score=cp.num(card.get("prev_score")),
                                           supplier_counts=supplier_counts,
                                           rnp=getattr(self, "rnp_by_inn", {}).get(inn, []))
        h = built.pop("input_hash")
        progress.note(f"сборка: рисков {len(built['risks'])}, балл {built['profile']['score']} ({built['profile']['zone']})")
        if not self.force and card.get("rm_status") == "completed" and card.get("prev_hash") == h:
            progress.note("входные данные не изменились — пересчёт не нужен")
            return {"card_id": cid, "status": "unchanged", "pushed": False}
        async with progress.step("резюме закупки" + (" (модель)" if self.llm else " (шаблон)")):
            resume, used_llm = await self._resume(built)
        pipeline = passport.pipeline_block(STAGE, "completed", self.model if used_llm else None,
                                           {"contract_resume": PROMPT_VERSION} if used_llm else {}, debug=self.force)
        pipeline.update(schema_version=cp.SCHEMA_VERSION, input_hash=h,
                        inputs={"files": len(item["files"]), "xml": len(item["xmls"]),
                                "rules": bool(item["values"]), "black_list": len(black_by_inn.get(inn, []))})
        ai = {**built, "resume": resume, "pipeline": pipeline, "analyzed_at": passport.now_iso()}
        result = {"card_id": cid, "status": "done", "pushed": False, "score": built["profile"]["score"],
                  "zone": built["profile"]["zone"], "risks": len(built["risks"]), "ai_analysis": ai}
        if self.push_results:
            try:
                async with progress.step("запись результата во внешнюю БД"):
                    await self.risk_api.patch_ai_analysis(EXTERNAL_TABLE, cid, ai)
                result["pushed"] = True
            except Exception as e:  # noqa: BLE001 — результат сохранён у нас
                logger.error(f"Не удалось записать ai_analysis закупки {cid}: {e}")
                result["push_error"] = str(e)
        async with progress.step("сохранение результата во внутренней БД"):
            await self._save_internal(cid, ai, result["pushed"], result.get("push_error"))
        return result

    async def _resume(self, built: Dict[str, Any]) -> Tuple[str, bool]:
        """Резюме закупки: модель (короткий запрос) или шаблон при её недоступности."""
        fallback = cp.default_resume(built["passport"], built["profile"], built["risks"], built["events"])
        if self.llm is None:
            return fallback, False
        p = built["passport"]
        payload = {
            "passport": {k: p.get(k) for k in ("purchase_number", "contract_number", "law", "stage", "method", "subject",
                                               "nmck", "contract_price", "price_reduction_pct", "applications",
                                               "customer", "supplier", "modification", "termination")},
            "profile": {k: built["profile"].get(k) for k in ("score", "zone", "delta", "components")},
            "risks": [{"title": r.get("title"), "description": (r.get("description") or "")[:300],
                       "level": r.get("level"), "sources": r.get("sources")} for r in built["risks"][:10]],
            "events": [{k: e.get(k) for k in ("title", "importance", "event_at")} for e in built["events"][:8]],
            "completeness": built["package"]["completeness"], "consistency": built["package"]["consistency"][:3],
            "registry": built["registry"]["matches"],
        }
        import json

        try:
            text = await self.llm.ask_text(RESUME_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str),
                                           max_tokens=1500, temperature=0.2)
            self.stats["llm_resume"] += 1
            return (text, True) if text else (fallback, False)
        except Exception as e:  # noqa: BLE001 — резюме не обязательно
            logger.warning(f"Резюме закупки {p.get('card_id')} шаблоном: {type(e).__name__}: {e}")
            return fallback, False

    async def _save_internal(self, cid: int, ai: Dict[str, Any], pushed: bool, error: Optional[str]) -> None:
        """Сохраняет результат у нас (``ai_rm_results``, ``kind = contract``)."""
        if self.repo is None or not hasattr(self.repo, "save_rm_result"):
            return
        try:
            await self.repo.save_rm_result(self.env, "contract", cid, ai, pushed, error)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Результат закупки {cid} не сохранён во внутренней БД: {type(e).__name__}: {e}")

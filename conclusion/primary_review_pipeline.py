"""Оценка заключений первичных экспертов по готовому сводному заключению (без отправки в основную БД).

Заключения берутся из внешней БД тем же способом, что в :mod:`conclusion.conclusion_pipeline` (SQL через
``DataFetcher``). Роли — по ``users.role``:

* **первичный эксперт** — ``expert``;
* **сводный эксперт** — ``raoExpert``; берётся последнее его заключение по ``updated_at``.

Для каждого первичного эксперта считаются расхождения с итоговым заключением, комментарий и предлагаемая оценка
(:mod:`knowledge_store.primary_compare`). Результат только возвращается; ``range`` и ``comment`` в основную БД
не записываются — их ставит сводный эксперт.
"""
import json
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from configs import settings
from configs.logger import get_logger
from knowledge_store import forms, primary_compare, primary_mention, primary_review, primary_score, primary_taxonomy

if TYPE_CHECKING:   # тяжёлые зависимости (aiohttp, pandas) нужны только классу загрузки, не чистой логике оценки
    from configs.http_client_manager import HTTPClientManager

logger = get_logger(__name__)

# Заключения экспертизы с ролью автора (``users.role``): ``expert`` — первичный, ``raoExpert`` — сводный.
SQL_OPINIONS = """
    SELECT o.id, o.expert_id, o.data, o.updated_at,
           e.object, e.type AS type_orig, e.checkType2,
           u.role AS expert_role, ee.accept, ee.`range` AS current_range
    FROM `expertise_expert_opinion7s` o
    INNER JOIN expertises e ON e.id = o.expertise_id
    INNER JOIN users u ON u.id = o.expert_id AND u.role IN ('expert', 'raoExpert')
    LEFT JOIN expertise_experts ee
           ON ee.expertise_id = o.expertise_id AND ee.expert_id = o.expert_id
    WHERE o.deleted_at IS NULL AND o.data IS NOT NULL AND o.data != CAST('[]' AS JSON)
      AND e.id = ?
"""
AMBIGUITY_GAP = 0.1          # разница вероятностей двух лучших оценок, при которой оценка считается неоднозначной
PRIMARY_ROLE, SUMMARY_ROLE = "expert", "raoExpert"      # users.role
LAW_BY_TYPE = {4: "44-ФЗ", 5: "223-ФЗ"}      # expertises.type: 4 — 44-ФЗ, 5 — 223-ФЗ


class PrimaryReviewError(Exception):
    """Ошибка, понятная вызывающему: ``http_status`` и ``message`` для ответа API."""

    def __init__(self, http_status: int, message: str):
        """Сохраняет код ответа и текст."""
        super().__init__(message)
        self.http_status, self.message = http_status, message


def _as_data(raw: Any) -> Optional[dict]:
    """Приводит ``data`` из ответа БД (строка JSON или словарь) к словарю; иначе ``None``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) and raw else None


def _is_blank(value: Any) -> bool:
    """Пустое значение из DataFrame (``None`` / ``NaN`` / ``'None'``)."""
    return value is None or value != value or str(value) in ("None", "nan", "")


def split_opinions(rows: List[dict], summary_opinion_id: Optional[int] = None) -> Dict[str, Any]:
    """Делит заключения экспертизы по роли автора (``users.role``) на первичные и итоговое сводное.

    Args:
        rows: Строки :data:`SQL_OPINIONS`.
        summary_opinion_id: ``id`` конкретной сохранённой версии итогового заключения (``expertise_expert_opinion7s.id``),
            например подтверждённой человеком. Если не задан, берётся последнее сводное по ``updated_at`` — такой расчёт
            предварительный: последняя сохранённая версия не обязательно подтверждена.

    Returns:
        dict: ``primary`` (список строк с ``data``), ``summary`` (выбранная строка сводного заключения или ``None``).

    Raises:
        PrimaryReviewError: 404 — версия ``summary_opinion_id`` не найдена среди сводных заключений экспертизы.
    """
    primary, summaries = [], []
    for row in rows:
        data = _as_data(row.get("data"))
        if data is None:
            continue
        item = {**row, "data": data}
        role = row.get("expert_role")
        if role == SUMMARY_ROLE:
            summaries.append(item)
        elif role == PRIMARY_ROLE:
            primary.append(item)
    if summary_opinion_id is not None:
        summary = next((r for r in summaries if int(r["id"]) == int(summary_opinion_id)), None)
        if summary is None:
            raise PrimaryReviewError(404, f"Версия итогового заключения {summary_opinion_id} не найдена у этой экспертизы")
    else:
        summary = max(summaries, key=lambda r: str(r.get("updated_at") or "")) if summaries else None
    return {"primary": primary, "summary": summary}


def review_expertise(rows: List[dict], labels: Optional[Dict[str, str]] = None,
                     extra_issues: Optional[Dict[int, List[primary_review.Issue]]] = None,
                     summary_opinion_id: Optional[int] = None) -> Dict[str, Any]:
    """Оценивает первичные заключения экспертизы по итоговому сводному (чистая функция, без БД).

    Args:
        rows: Строки :data:`SQL_OPINIONS`.
        labels: ``ключ поля → название критерия`` для комментария (необязательно).
        extra_issues: Дополнительные дефекты по ``expert_id`` (замечания модели из :mod:`knowledge_store.primary_llm_review`);
            попадают в ``issues``, ``categories`` и комментарий.
        summary_opinion_id: Версия итогового заключения для сравнения (см. :func:`split_opinions`); ``None`` — последняя сохранённая.

    Returns:
        dict: ``expertise_id`` не входит (добавляет вызывающий); ``summary_expert_id``, ``summary_opinion_id`` и ``summary_updated_at`` (версия итогового заключения, с которой сравнивали; по ним портал определяет, что подсказка устарела), ``is_preliminary`` (``True``, если версия не была указана явно и взята последняя сохранённая), ``group``, ``shared_differences`` (критерии, по которым все первичные эксперты расходятся с итоговым одинаково — вероятно, решение сводного эксперта; в комментарии они не упоминаются и помечены ``shared`` в ``differences``) и ``reviews`` —
        по записи на первичного эксперта: ``expert_id``, ``common``, ``differences`` (по видам), ``issues``,
        ``suggested_range`` (модель; ``None`` при малом числе общих критериев; снижена за замечания к заключению — ``issue_penalty_steps`` ступеней, прежняя оценка в ``range_before_issues`` и ``alternative_range``) с ``range_source`` и ``probabilities``, ``categories`` (замечания по таксономии), ``range_by_thresholds``, ``weighted_percent`` и ``deductions`` (взвешенный процент сходства и вычеты по расхождениям; по нему при ``range_source = weighted_percent`` определяется оценка), ``match_percent`` и ``range_by_scale`` (шкала аналитика), ``comment`` (согласован с оценкой, без нумерации; при 1.00 равен ``comment_details``), ``comment_details`` (полный перечень расхождений), ``alternative_range``, ``confidence``, ``model_range`` (до постпроверки по порядку) и ``rule_applied``, ``current_range``.

    Raises:
        PrimaryReviewError: 404 — первичных заключений нет; 409 — сводное заключение ещё не заполнено.
    """
    parts = split_opinions(rows, summary_opinion_id)
    if not parts["primary"]:
        raise PrimaryReviewError(404, "Нет заключений первичных экспертов по этой экспертизе")
    if not parts["summary"]:
        raise PrimaryReviewError(409, "Сводное заключение ещё не заполнено: оценивать не с чем")
    summary = parts["summary"]
    reviews = []
    ordered = sorted(parts["primary"], key=lambda r: int(r["expert_id"]))
    comparisons = [primary_compare.compare(row["data"], summary["data"]) for row in ordered]
    shared = primary_compare.shared_keys(comparisons)      # одинаковое расхождение у всех первичных: проверить итоговое
    for row, cmp in zip(ordered, comparisons):
        issues = primary_review.review_opinion(row["data"])
        method_issue = primary_review.compare_method(row["data"], summary["data"])   # методологическая ошибка: метод НМЦК
        if method_issue:
            issues.append(method_issue)
        # анкетные факты и суммы неустойки отчётных документов: только в комментарий, в оценку не входят
        issues.extend(primary_review.compare_report_facts(row["data"], summary["data"], labels))
        issues.extend((extra_issues or {}).get(int(row["expert_id"]), []))
        current = row.get("current_range")
        feats = primary_compare.features(row["data"], summary["data"], cmp)
        model = primary_compare.predict_range(feats, cmp.group)
        threshold_range = primary_compare.suggest_range(cmp)
        # основной способ — взвешенный процент сходства и пороги по истории; многоклассовая модель и пороги числа расхождений — запасные
        weighted = (primary_score.evaluate(cmp) if getattr(settings, "PRIMARY_REVIEW_USE_WEIGHTED_PERCENT", True)
                    and threshold_range is not None else None)
        if weighted:
            model_grade = weighted["range"]
        else:
            model_grade = model["range"] if model and threshold_range is not None else threshold_range
        grade, rule_applied = primary_compare.apply_full_match_rule(
            model_grade, cmp, shared, percent=weighted["percent"] if weighted else None)   # 1.00 только при полном соответствии
        # замечания к самому заключению (факты, суммы, заглушки и т. п.) снижают оценку; прежняя оценка остаётся как вторая
        grade_before_issues = grade
        grade, issue_steps = primary_review.issue_penalty(grade, issues)
        probs = model["probabilities"] if model and threshold_range is not None else None
        ranked = sorted(probs.items(), key=lambda kv: -kv[1]) if probs else []
        # две соседние оценки почти равновероятны (разница меньше 0.1): показываем вторую, чтобы эксперт решил сам
        if issue_steps:     # оценка снижена за замечания: эксперт видит, какой она была бы без них
            alternative = grade_before_issues
        elif weighted:       # пограничный процент: вторая оценка — в сторону ближайшего порога
            alternative = (None if rule_applied else primary_score.neighbour_grade(
                weighted["percent"], weighted["spec"], getattr(settings, "PRIMARY_REVIEW_PERCENT_MARGIN", 2.0)))
        else:
            alternative = (float(ranked[1][0]) if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < AMBIGUITY_GAP
                           and not rule_applied else None)   # после постпроверки по порядку оценка определена правилом
        mention = primary_mention.probabilities(cmp.group, cmp.differences)        # вероятность упоминания критерия сводным
        mention_min = float(primary_mention.load_model().get(cmp.group, {}).get("min_probability", 0.0)) \
            if getattr(settings, "PRIMARY_REVIEW_MENTION_FILTER", True) else 0.0
        details = primary_compare.build_comment(cmp, labels, issues, shared=shared)    # полный перечень расхождений
        own = [(d.kind, d.key, d.primary) for d in cmp.differences if d.key not in shared]       # общие для всех первичных не считаем
        reviews.append({
            "expert_id": int(row["expert_id"]),
            "common": cmp.common,
            "differences": [{"key": d.key, "label": primary_compare.label_for(d.key, labels), "kind": d.kind,
                             "primary": d.primary, "summary": d.summary, "shared": d.key in shared,
                             "mention_probability": mention.get(d.key)}
                            for d in cmp.differences],
            "issues": [{"code": i.code, "severity": i.severity, "message": i.message, "keys": i.keys} for i in issues],
            # обученная модель точнее порогов (на кросс-валидации 58% / 55% против 41% / 52%); без модели или при малом
            # числе общих критериев — пороги
            "suggested_range": grade,
            "range_before_issues": grade_before_issues,      # оценка до снижения за замечания (совпадает с suggested_range без штрафа)
            "issue_penalty_steps": issue_steps,
            "alternative_range": alternative,
            "model_range": model_grade,
            "rule_applied": rule_applied,
            "confidence": ranked[0][1] if ranked else None,
            "range_source": ("weighted_percent" if weighted else "model" if model and threshold_range is not None
                             else ("thresholds" if threshold_range is not None else None)),
            # взвешенный процент сходства и вычеты по расхождениям (отрицательный вычет — расхождение, которое обычно прощают)
            "weighted_percent": weighted["percent"] if weighted else None,
            "deductions": [{**d, "label": primary_compare.label_for(d["key"], labels), "shared": d["key"] in shared}
                           for d in weighted["deductions"]] if weighted else [],
            "probabilities": probs,
            "categories": primary_taxonomy.categorize(own, [i.code for i in issues], cmp.group),
            "range_by_thresholds": threshold_range,
            "match_percent": cmp.match_percent,
            "range_by_scale": primary_compare.range_by_scale(cmp.match_percent),
            # при 1.00 в комментарий идёт полный перечень расхождений (comment_details), при остальных оценках — только
            # вероятные (отбор по mention_probability) с вводной фразой по оценке
            "comment": details if grade == 1.0 else primary_compare.build_comment(
                cmp, labels, issues, shared=shared, grade=grade, mention=mention, mention_min=mention_min),
            # полный перечень расхождений (без отбора и вводной фразы)
            "comment_details": details,
            "current_range": None if _is_blank(current) else float(current),
        })
    group = "report" if primary_review.is_report_data(summary["data"]) else "procurement"
    # критерии, по которым все первичные эксперты разошлись с итоговым одинаково (при двух и более первичных)
    shared_list = [{"key": k, "label": primary_compare.label_for(k, labels), "primary": pv, "summary": sv}
                   for k in sorted(shared, key=lambda x: [int(n) for n in re.findall(r"\d+", x)])
                   for pv, sv in [next((d.primary, d.summary) for d in comparisons[0].differences if d.key == k)]]
    return {"summary_expert_id": int(summary["expert_id"]), "summary_opinion_id": int(summary["id"]),
            "summary_updated_at": str(summary.get("updated_at") or ""), "is_preliminary": summary_opinion_id is None,
            "group": group, "shared_differences": shared_list, "reviews": reviews}


class PrimaryReviewPipeline:
    """Загрузка заключений из внешней БД и оценка первичных экспертов."""

    def __init__(self, http_manager: "HTTPClientManager", expertise_id: int, environment: Optional[str] = None,
                 summary_opinion_id: Optional[int] = None):
        """Создаёт загрузчик данных по настройкам окружения (как :class:`RaoConclusionPipeline`)."""
        from configs.config import Config
        from configs.data_fetcher import DataFetcher

        config = Config()
        db_config = config.get_database_config(environment)
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)
        self.expertise_id = expertise_id
        self.environment = environment
        self.summary_opinion_id = summary_opinion_id    # None — последняя сохранённая версия итогового ЭЗ (расчёт предварительный)

    async def fetch_rows(self) -> List[dict]:
        """Загружает заключения экспертизы вместе с признаком назначения.

        Returns:
            list[dict]: Строки :data:`SQL_OPINIONS`; пустой список, если заключений нет.
        """
        df = await self.data_fetcher.fetch_async_expertise_data(SQL_OPINIONS, bindings=[self.expertise_id])
        return [] if df.empty else df.to_dict("records")

    async def labels_for(self, rows: List[dict]) -> Dict[str, str]:
        """Названия критериев формы экспертизы из справочника хранилища знаний.

        Без справочника (хранилище выключено, форма не загружена, ошибка БД) возвращает пустой словарь:
        комментарий тогда называет критерии ключами полей.
        """
        try:
            from configs.working_with_db import get_async_db_connection
            from knowledge_store import repository
            from knowledge_store.safe import is_enabled
            if not is_enabled() or not rows:
                return {}
            first = rows[0]
            async with get_async_db_connection() as conn:
                code = forms.get_form_code(LAW_BY_TYPE.get(int(first["type_orig"])), first["checkType2"], first["object"],
                                           available=await repository.list_form_codes(conn))
                if not code:
                    return {}
                labels = {f["field_key"]: f["label"] for f in await repository.get_form_fields(conn, code)}
            for suffix, fixes in (getattr(settings, "PRIMARY_REVIEW_LABEL_OVERRIDES", {}) or {}).items():
                if code.endswith(suffix):         # известные ошибки справочника: см. комментарий в configs/settings.py
                    labels.update(fixes)
            return labels
        except Exception as exc:  # noqa: BLE001 — названия необязательны, оценка важнее
            logger.warning(f"Не удалось получить названия критериев: {exc}")
            return {}

    async def llm_issues(self, rows: List[dict]) -> Dict[int, List[primary_review.Issue]]:
        """Замечания модели к тексту каждого первичного заключения (категории из ``PRIMARY_REVIEW_LLM_CATEGORIES``).

        Сбой модели не прерывает оценку: по эксперту, чей вызов не удался, замечаний просто нет (предупреждение в логе).

        Args:
            rows: Строки :data:`SQL_OPINIONS`.

        Returns:
            dict: ``expert_id → [Issue]``.
        """
        from configs.llm_client import get_llm
        from knowledge_store import primary_llm_review
        from risk_monitoring.llm_json import LLMJson

        parts = split_opinions(rows, self.summary_opinion_id)
        if not parts["summary"]:
            return {}
        categories = tuple(getattr(settings, "PRIMARY_REVIEW_LLM_CATEGORIES", ("FORMAL",)))
        out: Dict[int, List[primary_review.Issue]] = {}
        try:
            client, model = get_llm()
            llm = LLMJson(client, model, rate=1.0, concurrency=2)
        except Exception as exc:  # noqa: BLE001 — без модели оценка всё равно выполняется
            logger.warning(f"Модель недоступна, замечания к тексту пропущены: {exc}")
            return {}
        for row in parts["primary"]:
            try:
                findings = await primary_llm_review.review(llm, row["data"], parts["summary"]["data"])
                out[int(row["expert_id"])] = primary_llm_review.to_issues(findings, categories)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Замечания к тексту expert_id={row['expert_id']} не получены: {exc}")
        return out

    async def run(self, with_llm: bool = False) -> Dict[str, Any]:
        """Оценивает первичные заключения экспертизы.

        Args:
            with_llm: Добавить замечания модели к тексту (формальные недостатки). Замедляет ответ: по вызову модели на каждого
                первичного эксперта; по умолчанию выключено.

        Returns:
            dict: Результат :func:`review_expertise` плюс ``expertise_id``.

        Raises:
            PrimaryReviewError: Нет заключений (404) или нет сводного (409).
        """
        rows = await self.fetch_rows()
        if not rows:
            raise PrimaryReviewError(404, "Заключения по экспертизе не найдены")
        extra = await self.llm_issues(rows) if with_llm else None
        result = review_expertise(rows, await self.labels_for(rows), extra_issues=extra,
                                  summary_opinion_id=self.summary_opinion_id)
        logger.info(f"Оценка первичных экспертов expertise_id={self.expertise_id}: {len(result['reviews'])} заключений")
        return {"expertise_id": self.expertise_id, **result}


def make_rao_comments(http_manager: "HTTPClientManager", expertise_id: int, environment: Optional[str] = None):
    """Фабрика комментариев РАО для :func:`knowledge_store.summary.generate_summary`.

    Заключения первичных экспертов берутся из внешней БД; сравниваются с **собранным** ``data`` (а не с сохранённым
    заключением сводного эксперта), по расхождениям строится черновик ``rao_comment`` (:mod:`knowledge_store.rao_comments`).

    Args:
        http_manager: Менеджер HTTP-клиентов задачи.
        expertise_id: ID экспертизы.
        environment: Окружение (``X-API-Database``).

    Returns:
        Корутина ``data → {"<поле>-<expert_id>": текст}`` (пустой словарь, если первичных заключений нет).
    """
    from knowledge_store import rao_comments

    async def build(data: Dict[str, Any]) -> Dict[str, str]:
        """Сравнивает первичные заключения экспертизы с ``data`` и возвращает комментарии РАО."""
        pipeline = PrimaryReviewPipeline(http_manager, expertise_id, environment)
        primary = [r for r in await pipeline.fetch_rows() if r.get("expert_role") == PRIMARY_ROLE]
        if not primary:
            return {}
        first = primary[0]
        # собранное заключение выступает «итоговым»: сохранённые заключения сводного эксперта в сравнение не берём
        summary = {"id": 0, "expert_id": 0, "expert_role": SUMMARY_ROLE, "data": data, "updated_at": "",
                   "object": first["object"], "type_orig": first["type_orig"], "checkType2": first["checkType2"]}
        result = review_expertise(primary + [summary], await pipeline.labels_for(primary))
        return rao_comments.from_reviews(result["reviews"])

    return build

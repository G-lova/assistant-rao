"""Поиск признаков риска в тексте документа (LLM + проверка цитат кодом).

LLM только находит признаки и цитирует текст. Код:
  * ограничивает коды каталогом и типом документа (``doc_kind.allowed_codes``): например, в печатной
    форме извещения не ищутся риски обеспечения и аванса — их считают правила внешней системы;
  * отбрасывает риски, цитаты которых нет в тексте (галлюцинации), и риски с уверенностью ниже порога;
  * отбрасывает риски, противоречащие сами себе («… соответствует законодательству»),
    и DOC-002 без признаков товарного знака, модели или производителя в цитате;
  * считает балл риска формулой, а не берёт его у модели.
"""
import asyncio
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple


from configs.logger import get_logger
from risk_monitoring import risk_catalog
from risk_monitoring.llm_json import LLMJson

logger = get_logger(__name__)

PROMPT_PATH = "prompts/risk_analysis_prompt.txt"
SCHEMA_PATH = "schemas/risk_analysis_schema.json"
CHUNK_TOKENS = 10000
MIN_CONFIDENCE = 0.8
FUZZY_THRESHOLD = 90
SEVERITY_WEIGHT = {"low": 10, "medium": 25, "high": 40}
CATEGORY_NAMES = {"DOC": "Документные", "FIN": "Финансовые", "PROC": "Процедурные", "CTR": "Контрактные",
                  "BEH": "Поведенческие", "AI": "AI"}


# Описание риска, в котором модель сама говорит, что нарушения нет
SELF_DENIAL_RE = re.compile(
    r"соответству\w*\s+(требованиям\s+)?(законодательств|закон|44-фз|223-фз|норм)"
    r"|не\s+(выявлен|обнаружен|установлен|содерж\w*\s+ограничени)"
    r"|отсутству\w*\s+(конкретные\s+|какие-либо\s+|дополнительные\s+)?(ограничени|нарушени|признак)"
    r"|в\s+пределах\s+(допустим|установленн|норм)"
    r"|нарушени\w*\s+нет",
    re.IGNORECASE,
)
# Признаки товарного знака, модели, артикула или производителя в цитате (для DOC-002)
BRAND_RE = re.compile(
    r"[®™]|\b[A-Za-z][A-Za-z0-9\-]{1,}\b|«[^»]{2,40}»|\"[^\"]{2,40}\""
    r"|\b[А-ЯЁ][а-яё]+[А-ЯЁ][а-яёА-ЯЁ]*\b"   # слитное написание с заглавной внутри: «КлериГель»
    r"|модел|артикул|марк[аиу]\b|товарн\w+\s+знак|производств\w+\s|производител|фирм\w*\s|бренд|торгов\w+\s+наименован",
    re.IGNORECASE,
)
# Близкие по смыслу коды: на одну цитату остаётся один код группы
SYNONYM_GROUPS = ({"DOC-006", "AI-003"}, {"DOC-005", "AI-005"}, {"DOC-002", "DOC-003"})
MAX_CODES_PER_FRAGMENT = 2
EIS_ID_RE = re.compile(r"идентификатор\s*:?\s*\d+|\bктру\b|\bокпд", re.IGNORECASE)


def self_denial(risk: Dict[str, Any]) -> bool:
    """Модель описывает риск словами «соответствует законодательству», «ограничений нет» и т. п.

    Args:
        risk: Риск из ответа модели (``title``, ``explanation``).

    Returns:
        bool: ``True`` — описание противоречит самому риску, риск отбрасывается.
    """
    return bool(SELF_DENIAL_RE.search(f"{risk.get('title') or ''} {risk.get('explanation') or ''}"))


def brand_like(fragment: str) -> bool:
    """Есть ли в цитате признаки товарного знака, модели, артикула или производителя.

    Идентификаторы позиций ЕИС, коды КТРУ и ОКПД2 признаком не считаются.

    Args:
        fragment: Цитата.

    Returns:
        bool: ``True``, если цитата может подтверждать DOC-002.
    """
    text = EIS_ID_RE.sub(" ", fragment or "")
    return bool(BRAND_RE.search(text))


def _norm(s: str) -> str:
    """Нормализация для сверки цитаты: OCR-текст приходит с литеральными \\n и \\"."""
    s = (s or "").replace("\\n", " ").replace('\\"', '"')
    return re.sub(r"\s+", " ", s).strip().lower()


class DocumentRiskAnalyzer:
    """
    Поиск признаков риска в тексте документа.

    LLM только находит признаки и цитирует текст. Всё остальное делает код:
      * цитата обязана реально присутствовать в тексте (иначе риск отбрасывается как галлюцинация);
      * балл риска считается формулой (вес severity × confidence), а не выдумывается моделью.
    """

    def __init__(self, llm_client, model: str):
        """Загружает промпт и схему, подставляет каталог кодов документа, считает версию промпта.

        Args:
            llm_client: OpenAI-совместимый асинхронный клиент.
            model: Модель.
        """
        self.client = llm_client
        self.model = model
        self.llm = LLMJson(llm_client, model, rate=1.5, concurrency=3)
        self.semaphore = asyncio.Semaphore(3)

        self.codes = list(risk_catalog.DOCUMENT_RISK_CODES)
        with open(PROMPT_PATH, "rb") as f:
            raw = f.read()
        self.prompt = raw.decode("utf-8").replace("RISK_CATALOG", risk_catalog.catalog_text(self.codes))
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            schema_text = f.read()
        self.schema = json.loads(schema_text.replace('"RISK_CODES"', json.dumps(self.codes)))

        # Изменили промпт или схему -> кэш риск-анализа инвалидируется сам
        self.prompt_version = hashlib.sha256(
            self.prompt.encode("utf-8") + json.dumps(self.schema, sort_keys=True).encode()).hexdigest()[:12]

    # ------------------------------------------------------------------ public

    async def analyze(self, text: str, file_name: str, context: Optional[Dict[str, Any]] = None,
                      codes: Optional[List[str]] = None) -> Dict[str, Any]:
        """Анализирует документ по чанкам и объединяет найденные риски.

        Args:
            text: Полный текст документа.
            file_name: Имя файла (в промпт и логи).
            context: Контекст: тип документа, краткое содержание, паспорт закупки, закон.
            codes: Коды, которые разрешено выставлять для документа этого типа (по умолчанию — все коды документа).

        Returns:
            Dict[str, Any]: ``risk_score`` (0–100), ``risk_level``, ``summary``, ``risks``, ``partial``
            (часть чанков не проанализирована), ``dropped_unverified``, ``prompt_version``.

        Raises:
            RuntimeError: Если не проанализирован ни один чанк.
        """
        from configs.utils import split_large_text  # ленивый импорт: тянет aiofiles, magic, tiktoken

        allowed = [c for c in (codes if codes is not None else self.codes) if c in self.codes]
        chunks = split_large_text(text, max_chunk_size=CHUNK_TOKENS)
        if not chunks:
            return self._empty(partial=True)
        if not allowed:
            result = self._empty(partial=False)
            result["summary"] = "Для документа этого типа поиск рисков в тексте не проводится."
            return result

        async def _one(i: int, chunk: str):
            async with self.semaphore:
                return await self._analyze_chunk(chunk, file_name, context or {}, i + 1, len(chunks), allowed)

        results = await asyncio.gather(*(_one(i, c) for i, c in enumerate(chunks)), return_exceptions=True)

        ok = [r for r in results if isinstance(r, dict)]
        for r in results:
            if isinstance(r, BaseException):
                logger.error(f"Риск-анализ чанка не удался ({file_name}): {r}")
        if not ok:
            raise RuntimeError("Риск-анализ не выполнен ни для одного чанка")

        risks = self._merge([risk for r in ok for risk in r["risks"]])
        score = self._score(risks)
        dropped = sum(r["dropped"] for r in ok)
        rejected = sum(r.get("rejected", 0) for r in ok)

        summary = ok[0]["summary"] if len(ok) == 1 else self._build_summary(risks)
        return {
            "risk_score": score,
            "risk_level": self._level(score),
            "summary": summary,
            "risks": risks,
            "partial": len(ok) < len(chunks),          # часть чанков не проанализирована
            "dropped_unverified": dropped,             # цитаты, которых нет в тексте
            "rejected_inconsistent": rejected,         # риски, противоречащие сами себе или цитате
            "allowed_codes": allowed,
            "prompt_version": self.prompt_version,
        }

    def carry_over(self, previous: Optional[Dict[str, Any]], text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Риски прошлой редакции, цитаты которых остались в новой (анализ только изменений).

        Риск, цитата которого исчезла или изменилась, не переносится: по правилу риск-мониторинга он
        «не выявлен в новой версии» и считается снятым (``risks_comparison.not_found_in_new_version``),
        если не найден заново в изменённых фрагментах.

        Args:
            previous: Полный результат :meth:`analyze` прошлой редакции.
            text: Текст новой редакции.

        Returns:
            Tuple[List, List]: ``(перенесённые, снятые)`` риски в формате :meth:`analyze`.
        """
        norm_text = _norm(text)
        kept, dropped = [], []
        for r in (previous or {}).get("risks") or []:
            frag = ((r.get("evidence") or {}).get("fragment")) or ""
            (kept if self._fragment_in_text(frag, norm_text) else dropped).append(r)
        return kept, dropped

    def merge_results(self, kept: List[Dict[str, Any]], fresh: Optional[Dict[str, Any]],
                      allowed: List[str]) -> Dict[str, Any]:
        """Объединяет перенесённые риски и риски, найденные в изменённых фрагментах.

        Args:
            kept: Перенесённые риски (:meth:`carry_over`).
            fresh: Результат :meth:`analyze` по изменённым фрагментам (``None`` — изменений для анализа нет).
            allowed: Допустимые коды.

        Returns:
            Dict[str, Any]: Результат в формате :meth:`analyze` с ``incremental = True``.
        """
        fresh = fresh or {}
        risks = self._merge(list(kept) + list(fresh.get("risks") or []))
        score = self._score(risks)
        return {
            "risk_score": score,
            "risk_level": self._level(score),
            "summary": self._build_summary(risks) if risks else (fresh.get("summary") or "Признаков рисков не выявлено."),
            "risks": risks,
            "partial": bool(fresh.get("partial")),
            "dropped_unverified": fresh.get("dropped_unverified", 0),
            "rejected_inconsistent": fresh.get("rejected_inconsistent", 0),
            "allowed_codes": allowed,
            "prompt_version": self.prompt_version,
            "incremental": True,
            "carried_over": len(kept),
            "found_in_changes": len(fresh.get("risks") or []),
        }

    @staticmethod
    def to_compact(result: Dict[str, Any], top: int = 25) -> Dict[str, Any]:
        """Компактный вид результата (кэш и вход адаптера ``passport.to_external_risks``).

        Args:
            result: Результат :meth:`analyze`.
            top: Сколько рисков оставить (по убыванию веса).

        Returns:
            Dict[str, Any]: Балл, уровень, сводка и риски с цитатами, нормой и действиями эксперта.
        """
        return {
            "risk_score": result["risk_score"],
            "risk_level": result["risk_level"],
            "summary": result["summary"],
            "partial": result["partial"],
            "risks": [
                {
                    "code": r["code"],
                    "category": r["category"],
                    "severity": r["severity"],
                    "title": r["title"],
                    "explanation": r["explanation"],
                    "law": r.get("law"),
                    "verification_needed": r.get("verification_needed") or [],
                    "fragment": r["evidence"]["fragment"],
                    "section": r["evidence"].get("section"),
                    "page": r["evidence"].get("page"),
                    "confidence": r["evidence"]["confidence"],
                }
                for r in result["risks"][:top]
            ],
        }

    # ----------------------------------------------------------------- private

    async def _analyze_chunk(self, chunk: str, file_name: str, context: Dict[str, Any], n: int, total: int,
                             allowed: List[str]) -> Dict[str, Any]:
        """Анализ одного чанка: запрос к LLM, проверка кодов, уверенности, цитат и непротиворечивости.

        Args:
            chunk: Текст чанка.
            file_name: Имя файла.
            context: Контекст документа.
            n: Номер чанка.
            total: Всего чанков.
            allowed: Разрешённые коды.

        Returns:
            Dict[str, Any]: ``summary``, ``risks`` (только подтверждённые), ``dropped``, ``rejected``.
        """
        hint = json.dumps(context, ensure_ascii=False, default=str)[:2500] if context else "нет"
        user = (
            f"Документ: {file_name} (часть {n} из {total}).\n"
            f"Контекст (тип документа, краткое содержание, закупка, закон): {hint}\n"
            f"Для этого документа ищи ТОЛЬКО риски с кодами: {', '.join(allowed)}. Другие коды не используй.\n\n"
            f"Текст:\n\n{chunk}"
        )
        data = await self.llm.ask(self.prompt, user, self.schema, max_tokens=3000)

        norm_chunk = _norm(chunk)
        verified, dropped, rejected = [], 0, 0
        for r in data.get("risks", []):
            if not isinstance(r, dict) or r.get("code") not in allowed:
                continue
            ev = r.get("evidence") or {}
            frag = ev.get("fragment", "")
            if float(ev.get("confidence", 0) or 0) < MIN_CONFIDENCE or not frag.strip():
                continue
            if not self._fragment_in_text(frag, norm_chunk):
                dropped += 1
                continue
            if self_denial(r) or (r["code"] == "DOC-002" and not brand_like(frag)):
                rejected += 1
                continue
            r["category"] = risk_catalog.category(r["code"])
            ev["quote_verified"] = True
            verified.append(r)

        return {"summary": data.get("summary", ""), "risks": verified, "dropped": dropped, "rejected": rejected}

    @staticmethod
    def _fragment_in_text(fragment: str, norm_chunk: str) -> bool:
        """Есть ли цитата в тексте: точное вхождение после нормализации или нечёткое (≥ порога).

        Args:
            fragment: Цитата модели.
            norm_chunk: Нормализованный текст чанка.

        Returns:
            bool: ``True``, если цитата подтверждена.
        """
        nf = _norm(fragment)
        if not nf:
            return False
        if nf in norm_chunk:
            return True
        try:
            from fuzzywuzzy import fuzz  # ленивый импорт: модуль нужен только для нечёткой сверки
        except ImportError:  # pragma: no cover - fuzzywuzzy есть в requirements
            return False
        return fuzz.partial_ratio(nf, norm_chunk) >= FUZZY_THRESHOLD

    @staticmethod
    def _weight(r: Dict[str, Any]) -> float:
        """Вес риска: вес критичности × уверенность."""
        return SEVERITY_WEIGHT.get(r["severity"], 10) * float(r["evidence"]["confidence"])

    def _merge(self, risks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Убирает дубли из рисков разных чанков и сортирует по весу.

        * тот же код и начало цитаты — один риск;
        * одна цитата под близкими по смыслу кодами (``SYNONYM_GROUPS``: «неоднозначные требования»
          DOC-006 / AI-003, «избыточные требования» DOC-005 / AI-005) — остаётся код с большим весом;
        * на одну цитату — не больше ``MAX_CODES_PER_FRAGMENT`` кодов (в прогоне файла 3205 модель
          повесила 4 кода на один типовой пункт инструкции).
        """
        seen, groups, per_frag, out = set(), set(), {}, []
        for r in sorted(risks, key=self._weight, reverse=True):
            frag = _norm(r["evidence"]["fragment"])[:80]
            key = (r["code"], frag)
            group = next((i for i, g in enumerate(SYNONYM_GROUPS) if r["code"] in g), None)
            if key in seen or (group is not None and (group, frag) in groups):
                continue
            if per_frag.get(frag, 0) >= MAX_CODES_PER_FRAGMENT:
                continue
            seen.add(key)
            if group is not None:
                groups.add((group, frag))
            per_frag[frag] = per_frag.get(frag, 0) + 1
            out.append(r)
        return out

    def _score(self, risks: List[Dict[str, Any]]) -> int:
        """Балл риска документа 0–100: сумма весов рисков с ограничением сверху."""
        return min(100, round(sum(self._weight(r) for r in risks)))

    @staticmethod
    def _level(score: int) -> str:
        """Словесный уровень по баллу: none / low / medium / high."""
        if score == 0:
            return "none"
        return "low" if score < 25 else "medium" if score < 60 else "high"

    @staticmethod
    def _build_summary(risks: List[Dict[str, Any]]) -> str:
        """Сводка по рискам, если чанков несколько (без отдельного запроса к LLM)."""
        if not risks:
            return "Признаков риска не выявлено."
        titles = "; ".join(r["title"] for r in risks[:3])
        return f"Выявлено признаков риска: {len(risks)}. Наиболее значимые: {titles}."

    def _empty(self, partial: bool) -> Dict[str, Any]:
        """Пустой результат для документа без текста."""
        return {"risk_score": 0, "risk_level": "none", "summary": "Нет текста для анализа.",
                "risks": [], "partial": partial, "dropped_unverified": 0, "prompt_version": self.prompt_version}
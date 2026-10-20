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
    r"|нарушени\w*\s+нет"
    r"|исключа\w*\s+(риск|возможност)|не\s+влия\w*\s+на|не\s+затрагива|риск\w*\s+отсутству|риск\s+не\s+выявлен",
    re.IGNORECASE,
)
# Признаки товарного знака, модели, артикула или производителя в цитате (для DOC-002)
# Кавычки и короткие обозначения сами по себе товарный знак не доказывают: «триплекс» (материал), «О средствах
# массовой информации» (название закона), «Тип А2» (класс по ГОСТ) — ложные DOC-002 прогона 20.10.2026.
CAMEL_RE = re.compile(r"\b[А-ЯЁ][а-яё]+[А-ЯЁ][а-яёА-ЯЁ]*\b")   # слитное написание с заглавной внутри: «КлериГель»
BRAND_RE = re.compile(
    r"[®™]|\b[A-Za-z][A-Za-z0-9\-]{2,}\b"
    r"|\b[A-Za-z]{2,}[-\s]?\d+[A-Za-z0-9-]*\b|\b\d+[A-Za-z]+\b"      # HP 85A, X200, 85A (но не «А2»)
    r"|модел|артикул|марк[аиу]\b|товарн\w+\s+знак|производств\w+\s|производител|фирм\w*\s|бренд|торгов\w+\s+наименован",
    re.IGNORECASE,
)
QUOTED_RE = re.compile(r"«([^»]{2,40})»|\"([^\"]{2,40})\"")
# Название в кавычках — не товарный знак: закон, документ, порядок, правила; строчное слово (материал, тип)
NOT_BRAND_QUOTED_RE = re.compile(r"^(о|об|по|порядок|правила|положени|закон|стандарт|техническ|паспорт)\b|закон|кодекс",
                                 re.IGNORECASE)
NOT_BRAND_WORDS = {"iso", "gost", "usb", "hdmi", "led", "lcd", "din", "iec", "astm", "wi-fi", "wifi", "pdf", "html",
                   "http", "https", "www", "ghz", "mhz", "vga", "dvi", "ssd", "hdd", "ram", "dna", "pcr", "gmp",
                   "rohs", "atx", "hepa", "ulpa", "nsf", "led", "ups", "rus"}
# Цитаты типовых норм закона: требования, установленные законом, — не риск (prompts/risk_analysis_prompt.txt, п. 12–13)
STATUTORY_RE = re.compile(
    r"19\.28\s*кодекса|\bст\w*\.?\s*19\.28|независимая гарантия должна быть безотзывной"
    r"|указ\w*\s+президента\s+российской\s+федерации\s+от\s+03\.05\.2022|специальные экономические меры"
    r"|в соответствии с (п\.|пунктом|ч\.|частью)\s*[\d.]+\s*(ч\.|части)?\s*[\d.]*\s*(ст\.|статьи)\s*31"
    r"|«\s*или\s*»\s*,?\s*«\s*либо\s*»|«\s*не более\s*»|«\s*не менее\s*»|знак\w*\s*«?\s*[≥≤]"
    r"|не было привлечено к административной ответственности"
    r"|член\w*\s+(сро|саморегулируем)|саморегулируемой организации"
    r"|29\.12\.2021\s*№\s*2571|позици\w+\s+\d+\s+раздела|графы\s+\"?дополнительные требования"
    r"|не менее \d+ процентов начальной \(максимальной\) цены контракта"
    # ПП РФ № 1042: шкала штрафов «за каждый факт неисполнения или ненадлежащего исполнения…»
    r"|за каждый факт неисполнения"
    # ст. 14 и ПП РФ № 1875: подтверждение страны происхождения, евразийский реестр, участники ЕАЭС
    r"|стран\w*\s+происхождения|евразийск\w+\s+реестр|государств\w*\s*-?\s*член\w*\s+евразийского экономического союза"
    r"|стади\w+\s+производства|территории\s+еаэс"
    # ч. 2 ст. 43: конкретные показатели предложения участника
    r"|конкретные значения показателей"
    # диапазон «≥ … ≤ …» из КТРУ / описания объекта — не сужает круг товаров
    r"|[≥≤]\s*\d[^≥≤]{0,40}[≥≤]\s*\d"
    # ч. 6 ст. 34 и ПП № 1042 (пени), ст. 95 (односторонний отказ), казначейское сопровождение, ч. 5 ст. 30 (СМП),
    # ст. 45 и ПП № 1005 (независимая гарантия), ч. 13 ст. 94 (приёмка до 20 рабочих дней)
    r"|в случае просрочки исполнения (поставщиком|заказчиком|подрядчиком|исполнителем)"
    r"|решени\w+ об одностороннем отказе от исполнения"
    r"|казначейск\w+\s+сопровождени"
    r"|субподрядчик\w*[^.]{0,80}(смп|соно|малого предпринимательства)|subcontractorsattraction"
    r"|независим\w+\s+гаранти|гарантийного случая"
    r"|не позднее 20 \(двадцати\) рабочих дней",
    re.IGNORECASE,
)
STATUTORY_CODES = {"DOC-004", "DOC-005", "DOC-006", "AI-003", "AI-005", "PROC-012", "DOC-007", "DOC-003", "FIN-016",
                   "FIN-012", "AI-001", "DOC-001"}
GARBLED_WORD_RE = re.compile(r"[А-ЯЁа-яёA-Za-z]{3,}")


def garbled(fragment: str) -> bool:
    """Цитата — мусор OCR (скан без текстового слоя): осмысленных слов меньше трети (ДС 1.PDF, 20.10.2026)."""
    tokens = (fragment or "").split()
    if len(tokens) < 6:
        return False
    words = [t for t in tokens if GARBLED_WORD_RE.fullmatch(t.strip(".,;:!?«»\"()[]"))]
    return len(words) / len(tokens) < 0.35
SECURITY_PCT_RE = re.compile(r"обеспечени\w*[^.]{0,120}?(\d+(?:[.,]\d+)?)\s*(%|процент)", re.IGNORECASE)


def security_in_range(risk: Dict[str, Any]) -> bool:
    """FIN-005 / FIN-006 на обеспечении в пределах закона: исполнение контракта 0,5–30 % (ч. 6 ст. 96 44-ФЗ),
    гарантийные обязательства до 10 % — не «недостаточное обеспечение» (2807 прогона 20.10.2026: «5 % от НМЦК»).

    Args:
        risk: Риск модели.

    Returns:
        bool: ``True`` — отбросить.
    """
    if risk.get("code") not in ("FIN-005", "FIN-006", "FIN-007", "DOC-006"):
        return False
    m = SECURITY_PCT_RE.search(((risk.get("evidence") or {}).get("fragment")) or "")
    if not m:
        return False
    pct = float(m.group(1).replace(",", "."))
    return 0.5 <= pct <= (10 if risk["code"] == "FIN-006" else 30)


# строка таблицы обоснования НМЦК с суммами поставщиков («Итого 130000000.0 130000000.0 …», 3070 20.10.2026)
TABLE_TOTAL_RE = re.compile(r"(^|\s)(итого|всего)\b[^а-яё]*(\d[\d\s]*[.,]\d{1,2}[^\d]+){2,}|(\d{4,}(?:[.,]\d{1,2})?\s+){3,}",
                            re.IGNORECASE)


def table_total(risk: Dict[str, Any]) -> bool:
    """«Противоречие» в строке итогов таблицы с несколькими суммами (столбцы разных поставщиков в
    обосновании НМЦК — файл 1097): для DOC-007 / AI-002 / FIN-015 такая цитата не доказывает риск.

    Args:
        risk: Риск модели.

    Returns:
        bool: ``True`` — отбросить.
    """
    if risk.get("code") not in ("DOC-007", "AI-002", "FIN-015"):
        return False
    return bool(TABLE_TOTAL_RE.search(((risk.get("evidence") or {}).get("fragment")) or ""))


def statutory(risk: Dict[str, Any]) -> bool:
    """Цитата риска — типовая норма закона или типовая формулировка инструкции (риск не выставляется).

    Args:
        risk: Риск модели (``code``, ``evidence.fragment``).

    Returns:
        bool: ``True`` — отбросить.
    """
    if risk.get("code") not in STATUTORY_CODES:
        return False
    return bool(STATUTORY_RE.search(((risk.get("evidence") or {}).get("fragment")) or ""))


# «Отсутствует X» при том, что X в документе есть: тема → признак её наличия в тексте (2807, 20.10.2026: «нет
# обеспечения исполнения» и «нет раздела об оплате» с цитатой п. 13.4, хотя п. 8.1 и раздел оплаты есть)
ABSENCE_RE = re.compile(r"отсутств|не\s+указан|не\s+предусмотрен|нет\s+(раздел|требовани|информаци|сведени)", re.I)
TOPICS = {
    "обеспечени\w*\s+(его\s+)?исполнени": re.compile(r"обеспечени\w*\s+(его\s+)?исполнени\w*[^.]{0,80}(\d+(?:[.,]\d+)?\s*(%|процент)|размер)", re.I),
    "обеспечени\w*\s+гарантийн": re.compile(r"обеспечени\w*\s+гарантийн\w*\s+обязательств", re.I),
    "оплат": re.compile(r"(порядок|срок\w*|услови\w*)\s+(и\s+\w+\s+)?оплат|оплата\s+(производится|осуществляется)", re.I),
    "при[её]мк": re.compile(r"при[её]мк\w*\s+(товар|работ|услуг|результат)|документ\w*\s+о\s+при[её]мке", re.I),
    "гаранти": re.compile(r"гарантийн\w+\s+(срок|обязательств)", re.I),
    "ответственност": re.compile(r"ответственност\w*\s+сторон|штраф|пен[яи]", re.I),
}


def absence_contradicted(r: Dict[str, Any], text: str) -> bool:
    """Риск «в документе отсутствует …», хотя всё названное в нём есть (проверка по полному тексту).

    Args:
        r: Риск модели (``title``, ``explanation`` / ``description``).
        text: Полный текст документа.

    Returns:
        bool: ``True`` — отбросить: каждая названная отсутствующей тема в тексте найдена.
    """
    claim = f"{r.get('title') or ''} {r.get('explanation') or r.get('description') or ''}"
    if not text or not ABSENCE_RE.search(claim):
        return False
    named = [rx for topic, rx in TOPICS.items() if re.search(topic, claim, re.I)]
    return bool(named) and all(rx.search(text) for rx in named)


def rejected_by_rules(r: Dict[str, Any]) -> bool:
    """Риск модели отбрасывается правилами: требование закона, «или эквивалент» у DOC-002, самоопровержение,
    типовая норма, строка итогов, обеспечение в пределах закона, DOC-002 без признаков товарного знака.

    Применяется и к свежему ответу модели, и к результату из кэша (правила уточняются, а кэш остаётся).

    Args:
        r: Риск модели (``code``, ``evidence.fragment``, ``requirement_source``, ``title``, ``explanation``).

    Returns:
        bool: ``True`` — отбросить.
    """
    frag = ((r.get("evidence") or {}).get("fragment")) or ""
    by_law = str(r.get("requirement_source") or "").strip().lower() == "закон"
    # DOC-002 — товарный знак БЕЗ оговорки «или эквивалент»; цитата с этой оговоркой риск опровергает
    with_equivalent = r.get("code") == "DOC-002" and re.search(r"эквивалент", frag, re.IGNORECASE)
    return bool(by_law or with_equivalent or self_denial(r) or statutory(r) or table_total(r) or security_in_range(r)
                or garbled(frag) or (r.get("code") == "DOC-002" and not brand_like(frag)))


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
    for m in QUOTED_RE.finditer(text):
        q = (m.group(1) or m.group(2) or "").strip()
        if q and q[0].isupper() and not NOT_BRAND_QUOTED_RE.search(q):
            return True
    if CAMEL_RE.search(text):   # без IGNORECASE: иначе любое слово похоже на «КлериГель»
        return True
    return any(m.group(0).lower() not in NOT_BRAND_WORDS for m in BRAND_RE.finditer(text))


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
        ctx = {k: v for k, v in (context or {}).items() if k != "norms"}
        hint = json.dumps(ctx, ensure_ascii=False, default=str)[:2500] if ctx else "нет"
        norms_block = (f"Нормы, установленные законом для документа этого типа, — это НЕ риски; риск — только то, что "
                       f"идёт сверх них или отступает от них:\n{context['norms']}\n\n") if (context or {}).get("norms") else ""
        user = (
            f"Документ: {file_name} (часть {n} из {total}).\n"
            f"Контекст (тип документа, краткое содержание, закупка, закон): {hint}\n"
            f"{norms_block}"
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
            if rejected_by_rules(r):
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
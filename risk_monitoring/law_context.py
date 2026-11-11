"""Тексты норм и положения заказчика в запрос риск-анализа (RAG по загруженным актам, ``legal_acts``).

Раньше модель писала нормы по памяти: устаревшие ставки (НДС 20 %), несуществующие части статей. Теперь к запросу
риск-анализа документа добавляются тексты норм **действующей на дату закупки** редакции (решение 11.11.2026):

* **подбор по документу** (:meth:`LawContext.for_document`) — для каждого чанка документа (``rm_tokens_800``,
  эмбеддинги уже посчитаны для поиска похожих документов) ищутся ближайшие чанки норм (``chunker = law_norms``)
  среди актов закона закупки (:data:`ACTS_BY_FZ`); норма получает балл = сумма сходств по чанкам документа (норма,
  близкая многим частям документа, важнее нормы, близкой одной фразе); в запрос — :data:`MAX_NORMS` лучших не ниже
  :data:`MIN_SIMILARITY`;
* **закреплённые нормы** (:data:`PINNED`) — то, что модель чаще всего цитирует по типу документа (ч. 13 ст. 34,
  ч. 6 ст. 96 44-ФЗ, п. 3 ст. 164 НК для контракта): добавляются, даже если поиск их не поднял;
* **редакция на дату закупки** (``date_public`` карточки): действовавшая на день извещения (ст. 4 44-ФЗ — к закупке
  применяется закон на дату извещения); без даты — сегодняшняя.

Для 223-ФЗ к запросу добавляется **положение о закупке заказчика** на ту же дату (:meth:`LawContext.for_clause`):
способы закупки по положению со сроками подачи заявок и ближайшие к документу пункты положения (``typal_points``).
По 223-ФЗ обеспечение, аванс, сроки и способы задаёт положение, а не закон — модель сверяет документ с ним.

Блок текста — в ``context["law_texts"]`` / ``context["clause"]`` (``DocumentRiskAnalyzer`` выносит их из JSON
контекста отдельными разделами), ``version`` — хэш выбранных норм и редакций: входит в версию кэша риск-анализа.
"""
import hashlib
import json
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from configs.logger import get_logger

logger = get_logger(__name__)

# акты по закону закупки (ключи rm_legal_acts.act_key); при неизвестном законе — объединение
ACTS_BY_FZ: Dict[str, List[str]] = {
    "44": ["44-ФЗ", "ПП-1042", "ПП-1875", "ПП-2571", "ПП-145", "ПП-1005", "ПП-2604", "ПП-1078", "НК", "ФБ-2026",
           "135-ФЗ"],
    "223": ["223-ФЗ", "ПП-1352", "ПП-908", "ПП-1211", "ПП-1875", "135-ФЗ", "НК"],
}

CONTRACT_TYPES = {"docProjContractFiles", "docContractPostTovarFiles", "docContractDoWorkFiles", "docContractNIRFiles",
                  "docDopConsentContractFiles"}
NOTICE_TYPES = {"docOpusObjectZacupFiles", "docTrebContentRequestFiles", "docIzvejenieFiles", "docPorViewOcenkFiles",
                "docTechDocFiles"}

# закреплённые нормы: (ключ акта, адрес нормы в rm_legal_norms.address) по группе типов документа
PINNED: Dict[str, Dict[str, List[str]]] = {
    "44": {
        "contract": ["ч. 13 ст. 34 44-ФЗ", "ч. 6 ст. 96 44-ФЗ", "ч. 13 ст. 94 44-ФЗ", "п. 3 ст. 164 НК"],
        "notice": ["ч. 1 ст. 33 44-ФЗ", "ч. 1 ст. 31 44-ФЗ"],
    },
    "223": {
        "contract": ["п. 3 ст. 164 НК"],
        "notice": ["ч. 6.1 ст. 3 223-ФЗ"],
    },
}

TOP_PER_CHUNK = 4          # ближайших норм на чанк документа
DOC_CHUNKS = 40            # сколько первых чанков документа участвуют в поиске (длинные сметы — не все)
MAX_NORMS = 6              # норм в запросе
MIN_SIMILARITY = 0.55      # ниже — норма не про этот документ
NORM_CHARS = 900           # символов нормы в запросе
CLAUSE_POINTS = 4          # пунктов положения в запросе
CLAUSE_POINT_CHARS = 700

RM_CHUNKER = "rm_tokens_800"
LAW_CHUNKER = "law_norms"
CLAUSE_CHUNKER = "typal_points"

# действующие на дату редакции актов закона закупки
SQL_EDITIONS = """
SELECT DISTINCT ON (act_key) id, act_key, document_id, edition_date
FROM rm_legal_acts
WHERE act_key = ANY($1::text[]) AND edition_date <= $2 AND document_id IS NOT NULL
ORDER BY act_key, edition_date DESC
"""

# ближайшие нормы для каждого чанка документа (LATERAL — по одному поиску на чанк, без индекса: норм ~10 тыс.)
SQL_NEAREST = """
WITH q AS (
    SELECT chunk_no, embedding FROM ai_document_chunks
    WHERE document_id = $1 AND embedding_model = $2 AND chunker = $3 AND embedding IS NOT NULL
    ORDER BY chunk_no LIMIT $4
)
SELECT n.document_id, n.heading, n.text, 1 - (n.embedding <=> q.embedding) AS similarity
FROM q CROSS JOIN LATERAL (
    SELECT c.document_id, c.heading, c.text, c.embedding FROM ai_document_chunks c
    WHERE c.document_id = ANY($5::bigint[]) AND c.chunker = $6 AND c.embedding_model = $2
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> q.embedding LIMIT $7
) n
"""

SQL_PINNED = """
SELECT a.act_key, a.edition_date, n.address, n.heading, n.text
FROM rm_legal_norms n JOIN rm_legal_acts a ON a.id = n.act_id
WHERE n.act_id = ANY($1::bigint[]) AND n.address = ANY($2::text[]) AND NOT n.lost_force
"""


def doc_group(detected_type: Optional[str]) -> Optional[str]:
    """Группа типа документа для закреплённых норм: ``contract`` / ``notice`` / ``None``."""
    if detected_type in CONTRACT_TYPES:
        return "contract"
    if detected_type in NOTICE_TYPES:
        return "notice"
    return None


def acts_for(fz: Any) -> List[str]:
    """Акты закона закупки (при неизвестном законе — акты обоих законов)."""
    key = str(fz) if fz is not None else None
    if key in ACTS_BY_FZ:
        return ACTS_BY_FZ[key]
    return list(dict.fromkeys(ACTS_BY_FZ["44"] + ACTS_BY_FZ["223"]))


def as_date(v: Any) -> Optional[date]:
    """Дата закупки из строки ``ГГГГ-ММ-ДД…`` / ``date`` (``None`` — нет или не разобрана)."""
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def rank_norms(rows: Sequence[Dict[str, Any]], min_similarity: float = MIN_SIMILARITY,
               limit: int = MAX_NORMS) -> List[Dict[str, Any]]:
    """Лучшие нормы по сумме сходств с чанками документа.

    Args:
        rows: ``document_id``, ``heading`` (адрес), ``text``, ``similarity`` — по строке на пару «чанк документа —
            норма».
        min_similarity: Порог сходства пары.
        limit: Сколько норм вернуть.

    Returns:
        List[Dict[str, Any]]: ``address``, ``text``, ``score`` (сумма сходств), ``best`` (лучшее сходство).
    """
    acc: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sim = float(r.get("similarity") or 0)
        if sim < min_similarity:
            continue
        key = r.get("heading") or ""
        a = acc.setdefault(key, {"address": key, "text": r.get("text") or "", "document_id": r.get("document_id"),
                                 "score": 0.0, "best": 0.0})
        a["score"] += sim
        a["best"] = max(a["best"], sim)
    return sorted(acc.values(), key=lambda a: (-a["score"], -a["best"], a["address"]))[:limit]


def _strip_address(text: str, address: str) -> str:
    """Чанк нормы начинается с «адрес (название статьи): » — в запросе адрес идёт отдельно."""
    t = text or ""
    if address and t.startswith(address):
        t = t[len(address):].lstrip()
        if t.startswith("("):
            end = t.find("):")
            if 0 < end < 200:
                t = t[end + 2:].lstrip()
        elif t.startswith(":"):
            t = t[1:].lstrip()
    return t


def format_norms(norms: Sequence[Dict[str, Any]], editions: Dict[str, str], chars: int = NORM_CHARS) -> str:
    """Блок норм для запроса: «[адрес] (ред. от …): текст» по строке на норму."""
    out = []
    for n in norms:
        ed = editions.get(n.get("act_key") or "")
        text = _strip_address(n.get("text") or "", n["address"])
        if len(text) > chars:
            text = text[:chars].rsplit(" ", 1)[0] + " …"
        out.append(f"[{n['address']}]" + (f" (ред. от {ed})" if ed else "") + f": {text}")
    return "\n".join(out)


def format_clause(clause: Dict[str, Any], points: Sequence[Dict[str, Any]], method: Optional[str]) -> str:
    """Блок положения заказчика: реквизиты, способы закупки со сроками подачи заявок, ближайшие пункты."""
    head = clause.get("head") or {}
    kind = ("присоединение к положению " + (head.get("name") or "другой организации")
            if clause.get("clause_type") == "customer" and clause.get("head_registration_number") else
            "собственное положение заказчика" if clause.get("clause_type") == "customer" else
            f"типовое положение ({clause.get('creator_name') or 'учредитель'})")
    lines = [f"Положение о закупке № {clause.get('registration_number')} ред. {clause.get('version')}, "
             f"применяется с {clause.get('use_for_base_date') or '—'}: {kind}."]
    methods = clause.get("purchase_methods") or []
    if isinstance(methods, str):
        try:
            methods = json.loads(methods)
        except ValueError:
            methods = []
    if methods:
        items = []
        for m in methods[:25]:
            days = m.get("application_days")
            items.append((m.get("name") or "—") + (f" (заявки не менее {days} {'раб.' if m.get('work_days') else 'кал.'} дн.)"
                                                    if days else "") + (", электронная" if m.get("electronic") else ""))
        lines.append("Способы закупки по положению: " + "; ".join(items) + ".")
        if method:
            lines.append(f"Способ этой закупки: {method}.")
    for p in points:
        text = (p.get("text") or "")[:CLAUSE_POINT_CHARS]
        lines.append(f"[п. {p.get('point') or '—'} положения]: {text}")
    return "\n".join(lines)


class LawContext:
    """Подбор норм и пунктов положения к документу; редакции на дату кэшируются на время жизни объекта.

    Args:
        pool: Пул ``asyncpg`` (``AIRepo.pool``).
        embedding_model: Модель эмбеддингов документов и норм.
    """

    def __init__(self, pool: Any, embedding_model: str):
        """Запоминает пул и модель, создаёт кэш редакций."""
        self.pool = pool
        self.model = embedding_model
        self._editions: Dict[tuple, List[Dict[str, Any]]] = {}

    async def editions(self, fz: Any, on: Optional[date]) -> List[Dict[str, Any]]:
        """Редакции актов закона закупки, действующие на дату (кэш по закону и дате)."""
        key = (str(fz), on)
        if key not in self._editions:
            rows = await self.pool.fetch(SQL_EDITIONS, acts_for(fz), on or date.today())
            self._editions[key] = [dict(r) for r in rows]
        return self._editions[key]

    async def for_document(self, document_id: int, fz: Any, on: Optional[date],
                           detected_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Нормы для запроса риск-анализа документа.

        Args:
            document_id: ``ai_documents.id`` документа (чанки ``rm_tokens_800`` с эмбеддингами).
            fz: Закон закупки (``44`` / ``223`` / ``None``).
            on: Дата закупки (редакция на дату); ``None`` — сегодня.
            detected_type: Тип документа (закреплённые нормы).

        Returns:
            Optional[Dict[str, Any]]: ``text`` (блок для запроса), ``refs`` (адреса), ``editions``
            (акт → дата редакции), ``on``, ``version``; ``None`` — актов нет или норм не нашлось.
        """
        eds = await self.editions(fz, on)
        if not eds:
            return None
        by_doc = {int(e["document_id"]): e for e in eds}
        rows = await self.pool.fetch(SQL_NEAREST, document_id, self.model, RM_CHUNKER, DOC_CHUNKS,
                                     list(by_doc), LAW_CHUNKER, TOP_PER_CHUNK)
        found = rank_norms([dict(r) for r in rows])
        for n in found:
            n["act_key"] = by_doc.get(int(n["document_id"]), {}).get("act_key")
        group = doc_group(detected_type)
        pinned_addr = (PINNED.get(str(fz), {}).get(group) or []) if group else []
        have = {n["address"] for n in found}
        missing = [a for a in pinned_addr if a not in have]
        if missing:
            for r in await self.pool.fetch(SQL_PINNED, [int(e["id"]) for e in eds], missing):
                found.append({"address": r["address"], "act_key": r["act_key"], "score": 0.0,
                              "text": f"{r['address']}" + (f" ({r['heading']})" if r["heading"] else "") + f": {r['text']}"})
        if not found:
            return None
        editions = {e["act_key"]: (e["edition_date"].isoformat() if hasattr(e["edition_date"], "isoformat")
                                   else str(e["edition_date"])) for e in eds}
        refs = [n["address"] for n in found]
        version = hashlib.sha256(json.dumps([refs, sorted(editions.items())], ensure_ascii=False)
                                 .encode("utf-8")).hexdigest()[:10]
        return {"text": format_norms(found, editions), "refs": refs, "editions": editions,
                "on": (on or date.today()).isoformat(), "version": version}

    async def for_clause(self, document_id: int, inn: Optional[str], kpp: Optional[str], on: Optional[date],
                         method: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Положение о закупке заказчика (223-ФЗ) на дату с ближайшими к документу пунктами.

        Returns:
            Optional[Dict[str, Any]]: ``text``, ``registration_number``, ``version``, ``clause_type``, ``points``,
            ``hash``; ``None`` — положение заказчика не загружено.
        """
        if not inn:
            return None
        from risk_monitoring.typal_clauses import TypalRepo

        clause = await TypalRepo(self.pool).customer_clause(str(inn), kpp or None, (on or date.today()).isoformat())
        if not clause:
            return None
        points: List[Dict[str, Any]] = []
        pos = clause.get("position") or {}
        if pos.get("document_id"):
            rows = await self.pool.fetch(SQL_NEAREST, document_id, self.model, RM_CHUNKER, DOC_CHUNKS,
                                         [int(pos["document_id"])], CLAUSE_CHUNKER, 2)
            points = [{"point": r["address"], "text": r["text"]}
                      for r in rank_norms([dict(r) for r in rows], limit=CLAUSE_POINTS)]
        text = format_clause(clause, points, method)
        h = hashlib.sha256(json.dumps([clause.get("registration_number"), clause.get("version"),
                                       [p["point"] for p in points]], ensure_ascii=False, default=str)
                           .encode("utf-8")).hexdigest()[:10]
        return {"text": text, "registration_number": clause.get("registration_number"),
                "version": clause.get("version"), "clause_type": clause.get("clause_type"),
                "points": [p["point"] for p in points], "hash": h}

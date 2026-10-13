"""Связь экспертизы с риск-мониторингом: карточка закупки и находки РМ для эксперта.

1. Идентификаторы закупки экспертизы (реестровый номер из ссылки ЕИС, номер контракта) сохраняются в
   ``procurement_identity`` во время ``/evaluate-documents``.
2. В фоне (:func:`match_card`, задача ``index_documents_task``) по ним находится карточка
   ``risk_monitoring_contracts`` во внешней БД — теми же запросами, что привязка «сирот» в РМ:
   номер закупки → номер контракта. Ссылки РМ на файлы этой закупки продлеваются на срок экспертизы.
3. :func:`findings` собирает находки РМ (``ai_document_analysis`` типа ``risk_analysis*``) по документам
   экспертизы и по остальным документам той же закупки в РМ. Это подсказки эксперту: значения заключения
   они не меняют.
"""
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from configs.config import Config
from configs.logger import get_logger
from knowledge_store.scope import Scope

logger = get_logger(__name__)

NOTE = ("Находки риск-мониторинга по документам закупки — подсказки для эксперта. Значения заключения "
        "они не меняют; цитата и страница указывают, где проверить.")

SQL_SAVE_IDENTIFIERS = """
INSERT INTO procurement_identity (env, expertise_id, eis_number, contract_number)
VALUES ($1, $2, $3, $4)
ON CONFLICT (env, expertise_id) DO UPDATE SET
    eis_number = COALESCE(EXCLUDED.eis_number, procurement_identity.eis_number),
    contract_number = COALESCE(EXCLUDED.contract_number, procurement_identity.contract_number),
    updated_at = NOW()
"""
SQL_GET_IDENTITY = """
SELECT eis_number, contract_number, rm_card_id, match_method, candidates, matched_at
FROM procurement_identity WHERE env = $1 AND expertise_id = $2
"""
SQL_SET_MATCH = """
UPDATE procurement_identity
SET rm_card_id = $3, match_method = $4, candidates = $5::jsonb, matched_at = NOW(), updated_at = NOW()
WHERE env = $1 AND expertise_id = $2
"""
# Пока идёт экспертиза, тексты файлов закупки в РМ не очищаются: срок ссылок не меньше срока экспертизы
SQL_EXTEND_RM_LINKS = """
UPDATE ai_file_links SET expires_at = GREATEST(COALESCE(expires_at, NOW()), NOW() + make_interval(days => $3))
WHERE env = $1 AND rm_card_id = $2
"""
SQL_FINDINGS = """
WITH exp_docs AS (
    SELECT d.ai_document_id AS document_id, min(d.filename) AS filename, min(d.doc_code) AS doc_code
    FROM pe_documents d
    WHERE d.env = $1 AND d.expertise_id = $2 AND d.ai_document_id IS NOT NULL
    GROUP BY d.ai_document_id
), card_docs AS (
    SELECT l.document_id, array_agg(l.external_file_id ORDER BY l.external_file_id) AS external_file_ids
    FROM procurement_identity i JOIN ai_file_links l ON l.env = i.env AND l.rm_card_id = i.rm_card_id
    WHERE i.env = $1 AND i.expertise_id = $2 AND i.rm_card_id IS NOT NULL
    GROUP BY l.document_id
), docs AS (
    SELECT COALESCE(e.document_id, c.document_id) AS document_id, e.filename, e.doc_code, c.external_file_ids,
           CASE WHEN e.document_id IS NOT NULL THEN 'expertise' ELSE 'rm_card' END AS origin
    FROM exp_docs e FULL JOIN card_docs c ON c.document_id = e.document_id
)
SELECT DISTINCT ON (docs.document_id)
       docs.document_id, docs.filename, docs.doc_code, docs.external_file_ids, docs.origin,
       a.analysis_type, a.compact, a.model, a.created_at
FROM docs JOIN ai_document_analysis a ON a.document_id = docs.document_id AND a.analysis_type LIKE 'risk_analysis%'
ORDER BY docs.document_id, a.created_at DESC
"""

Fetch = Callable[..., Awaitable[List[Dict[str, Any]]]]


def _clean_number(value: Any) -> Optional[str]:
    """Номер закупки/контракта как строка без пробелов (``None``, NaN и пустые значения — ``None``)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    return text


async def save_identifiers(conn, scope: Scope, eis_number: Any = None, contract_number: Any = None) -> None:
    """Запоминает идентификаторы закупки экспертизы (пустые значения не затирают известные).

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        eis_number: Реестровый номер из ссылки ЕИС.
        contract_number: Номер контракта из карточки экспертизы.
    """
    eis, contract = _clean_number(eis_number), _clean_number(contract_number)
    if eis or contract:
        await conn.execute(SQL_SAVE_IDENTIFIERS, scope.env, scope.expertise_id, eis, contract)


async def get_identity(conn, scope: Scope) -> Optional[dict]:
    """Идентификаторы и результат сопоставления экспертизы.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        Optional[dict]: Строка ``procurement_identity`` или ``None``.
    """
    rows = await conn.fetch(SQL_GET_IDENTITY, scope.env, scope.expertise_id)
    return dict(rows[0]) if rows else None


def _int(value: Any) -> Optional[int]:
    """Целое из значения внешней БД или ``None``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def find_card(fetch: Fetch, numbers: Sequence[str]) -> Dict[str, Any]:
    """Карточка закупки РМ по номерам: сначала как номер закупки, затем как номер контракта.

    Args:
        fetch: ``async (имя_запроса, nums=[...]) -> строки`` — запросы ``queries/rm/*.sql`` к внешней БД.
        numbers: Номера (реестровый номер из ЕИС, номер контракта).

    Returns:
        dict: ``{"method", "rm_card_id", "candidates"}``; ``method = none`` — не найдено.
    """
    nums = [n for n in dict.fromkeys(_clean_number(x) for x in numbers) if n]
    if not nums:
        return {"method": "none", "rm_card_id": None, "candidates": []}
    cards = await fetch("link_by_number.sql", nums=nums)
    ids = [i for i in (_int(c.get("id")) for c in cards) if i]
    if ids:
        return {"method": "purchase_number", "rm_card_id": ids[0], "candidates": ids}
    rows = await fetch("link_by_contract_number.sql", nums=nums)
    rows = sorted(rows, key=lambda r: -(_int(r.get("n")) or 0))
    ids = [i for i in (_int(r.get("risk_monitoring_contract_id")) for r in rows) if i]
    if ids:
        return {"method": "contract_number", "rm_card_id": ids[0], "candidates": list(dict.fromkeys(ids))}
    return {"method": "none", "rm_card_id": None, "candidates": []}


def external_fetch(http_manager: Any, env: str) -> Fetch:
    """Функция запросов ``queries/rm/*.sql`` к внешней БД окружения (только чтение).

    Args:
        http_manager: Общий ``HTTPClientManager``.
        env: Окружение (``X-API-Database``).

    Returns:
        Fetch: ``async (имя, **списки) -> list[dict]``.
    """
    from configs.data_fetcher import DataFetcher
    from risk_monitoring import rm_sql

    db = Config().get_database_config(env)
    fetcher = DataFetcher(db.url, db.headers, http_manager)

    async def fetch(name: str, **lists: Sequence[Any]) -> List[Dict[str, Any]]:
        """Выполняет запрос и возвращает строки."""
        sql, bindings = rm_sql.render(name, **lists)
        df = await fetcher.fetch_async_expertise_data(sql, bindings=bindings)
        return [] if df is None or df.empty else df.to_dict("records")

    return fetch


async def match_card(conn, scope: Scope, fetch: Fetch) -> Dict[str, Any]:
    """Сопоставляет экспертизу с карточкой РМ и продлевает срок ссылок РМ на файлы этой закупки.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        fetch: Запросы к внешней БД (:func:`external_fetch`).

    Returns:
        dict: ``{"method", "rm_card_id", "candidates", "extended_links"}`` либо ``{"skipped": причина}``.
    """
    identity = await get_identity(conn, scope)
    if not identity:
        return {"skipped": "нет номеров закупки и контракта"}
    found = await find_card(fetch, [identity.get("eis_number"), identity.get("contract_number")])
    await conn.execute(SQL_SET_MATCH, scope.env, scope.expertise_id, found["rm_card_id"], found["method"],
                       json.dumps(found["candidates"]))
    found["extended_links"] = 0
    if found["rm_card_id"]:
        status = await conn.execute(SQL_EXTEND_RM_LINKS, scope.env, int(found["rm_card_id"]),
                                    Config.PE_TEXT_RETENTION_DAYS)
        found["extended_links"] = _affected(status)
    logger.info(f"knowledge_store: экспертиза {scope} ↔ РМ: {found}")
    return found


def _affected(status: Any) -> int:
    """Число строк из статуса ``asyncpg.execute`` (``'UPDATE 3'``); для тестовых соединений — 0."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


def _load(value: Any) -> Any:
    """JSONB из драйвера: asyncpg без кодека возвращает строку."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def findings(conn, scope: Scope) -> Dict[str, Any]:
    """Находки РМ для эксперта: по документам экспертизы и по остальным документам той же закупки в РМ.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        dict: ``note``, ``rm_card_id``, ``match_method``, ``documents`` — по документу: происхождение
        (``expertise`` / ``rm_card``), имя файла или id файлов РМ, уровень и балл риска, сводка и риски
        (код, название, критичность, пояснение, норма, цитата, страница, уверенность, что проверить).
    """
    identity = await get_identity(conn, scope) or {}
    out: Dict[str, Any] = {"note": NOTE, "rm_card_id": identity.get("rm_card_id"),
                           "match_method": identity.get("match_method"), "documents": []}
    for row in await conn.fetch(SQL_FINDINGS, scope.env, scope.expertise_id):
        row = dict(row)
        compact = _load(row.get("compact")) or {}
        risks = [r for r in (compact.get("risks") or []) if isinstance(r, dict)]
        if not risks and not compact.get("summary"):
            continue
        out["documents"].append({
            "origin": row.get("origin"),
            "filename": row.get("filename"),
            "doc_code": row.get("doc_code"),
            "external_file_ids": list(_load(row.get("external_file_ids")) or []),
            "analysis": row.get("analysis_type"),
            "model": row.get("model"),
            "analyzed_at": str(row.get("created_at")) if row.get("created_at") else None,
            "risk_level": compact.get("risk_level"),
            "risk_score": compact.get("risk_score"),
            "summary": compact.get("summary"),
            "risks": [{k: r.get(k) for k in ("code", "category", "severity", "title", "explanation", "law",
                                             "fragment", "section", "page", "confidence", "verification_needed")}
                      for r in risks],
        })
    out["documents"].sort(key=lambda d: (d["origin"] != "expertise", -(d.get("risk_score") or 0)))
    return out

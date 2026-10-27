"""Типовые положения о закупке (223-ФЗ) из ЕИС: разбор ``typalOrderClause``, хранение версий, положение заказчика.

Типовое положение утверждает орган субъекта (например, комитет по регулированию контрактной системы
Волгоградской области, приказ от 10.06.2019 № 9н). В ЕИС оно публикуется документом ``typalOrderClause``:

* ``registrationNumber`` и ``version`` — реестровый номер и номер редакции (каждое изменение — новая версия);
* ``approveDate`` — дата утверждения, ``useForBaseDate`` — с какой даты редакция применяется;
* ``modificationReason`` — приказ об изменениях;
* ``purchaseMethod[]`` — способы закупки и условия их применения;
* ``customerInfo[]`` — заказчики, для которых положение обязательно (ИНН, КПП, тип учреждения:
  ``budgetAgencyType`` — бюджетное, ``autonomousAgencyType`` — автономное);
* ``attachments/document[]`` — файлы: сами положения (для ГАУ, ГБУ …), перечни учреждений, приказы.

Мы храним каждую версию (``rm_typal_clauses``), её заказчиков (``rm_typal_clause_customers``) и файлы
(``rm_typal_clause_files``); текст файлов — общий документ ``ai_documents``, нарезанный по пунктам (``typal_points``)
с эмбеддингами — для проверки рисков 223-ФЗ по положению конкретного заказчика. Положение заказчика —
файл-положение действующей на дату версии, соответствующий типу учреждения заказчика.

Загрузка: ``getDocsByOrgRegionRequest`` (подсистема ``settings.TYPAL_SUBSYSTEM``, тип документа
``settings.TYPAL_DOCUMENT_TYPE``, регион — код ФНС) за дату; или XML вручную (``load_xml``).
"""
import hashlib
import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from configs.logger import get_logger

logger = get_logger(__name__)

CHUNKER = "typal_points"
CHUNK_CHARS = 1800
POINT_RE = re.compile(r"(?m)^\s*(\d{1,2}(?:\.\d{1,3}){0,3})\.?\s+(?=\S)")

AGENCY_NAME_RE = [
    ("autonomous", re.compile(r"\bГАУ\b|автономн", re.I)),
    ("budget", re.compile(r"\bГБУ\b|бюджетн", re.I)),
    ("treasury", re.compile(r"\bГКУ\b|казенн|казённ", re.I)),
    ("unitary", re.compile(r"\b[ГМ]УП\b|унитарн", re.I)),
]
# перечень проверяется первым: в его названии бывает «…применение типового положения…»
KIND_NAME_RE = [
    ("list", re.compile(r"перечен|перечн", re.I)),
    ("position", re.compile(r"типов\w*\s+положени|положени\w*\s+о\s+закупк", re.I)),
    ("order", re.compile(r"приказ|изменени|распоряжени|постановлени", re.I)),
]


def _list(v: Any) -> List[Any]:
    """Узел XML как список (один элемент ``xmltodict`` отдаёт словарём)."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _date(v: Any) -> Optional[str]:
    """``2026-03-28T00:00:00`` → ``2026-03-28``."""
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return m.group(0) if m else None


def agency_from_name(name: str) -> Optional[str]:
    """Тип учреждения по названию файла или организации (ГАУ / ГБУ / ГКУ / ГУП)."""
    for kind, rx in AGENCY_NAME_RE:
        if rx.search(name or ""):
            return kind
    return None


def file_kind(name: str) -> str:
    """Вид файла: ``position`` (положение), ``list`` (перечень), ``order`` (приказ, изменения), ``other``."""
    for kind, rx in KIND_NAME_RE:
        if rx.search(name or ""):
            return kind
    return "other"


def customer_agency(main: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Тип и уровень учреждения заказчика из ``mainInfo``.

    Returns:
        Dict[str, Optional[str]]: ``agency_kind`` (``budget`` / ``autonomous`` / ``treasury`` / ``unitary`` / ``None``),
        ``agency_level`` (``SUBJECT`` / ``MUNICIPAL`` / ``STATE`` …).
    """
    if main.get("budgetAgencyType"):
        return {"agency_kind": "budget", "agency_level": main.get("budgetAgencyType")}
    if main.get("autonomousAgencyType"):
        return {"agency_kind": "autonomous", "agency_level": main.get("autonomousAgencyType")}
    return {"agency_kind": agency_from_name(str(main.get("okopfName") or "") + " " + str(main.get("fullName") or "")),
            "agency_level": None}


def parse_typal_xml(raw: bytes) -> List[Dict[str, Any]]:
    """Разбирает XML ``typalOrderClause`` (в одном файле может быть несколько ``item``).

    Args:
        raw: Байты XML.

    Returns:
        List[Dict[str, Any]]: Версии положения: реквизиты, ``purchase_methods``, ``customers``, ``files``.

    Raises:
        ValueError: Это не ``typalOrderClause`` или XML некорректен.
    """
    from risk_monitoring.xml_events import parse_xml

    tag, _, body = parse_xml(raw)
    if tag != "typalOrderClause":
        raise ValueError(f"ожидался typalOrderClause, получен {tag}")
    sha = hashlib.sha256(raw).hexdigest()
    out = []
    for item in _list((body.get("body") or {}).get("item")):
        d = (item or {}).get("typalOrderClauseData") or {}
        if not d:
            continue
        creator = ((d.get("creator") or {}).get("mainInfo")) or {}
        customers = []
        for c in _list(d.get("customerInfo")):
            m = (c or {}).get("mainInfo") or {}
            if not m.get("inn"):
                continue
            customers.append({"inn": str(m["inn"]).strip(), "kpp": str(m.get("kpp") or "").strip(),
                              "ogrn": m.get("ogrn"), "name": m.get("fullName"), "short_name": m.get("shortName"),
                              "region": m.get("region"), **customer_agency(m)})
        files = []
        for doc in _list((d.get("attachments") or {}).get("document")):
            name = str(doc.get("fileName") or "")
            descr = str(doc.get("description") or "")
            files.append({"file_name": name, "description": descr, "url": doc.get("url"),
                          "content_uid": doc.get("contentUid"), "kind": file_kind(name + " " + descr),
                          "agency_kind": agency_from_name(name + " " + descr)})
        methods = [{"code": m.get("purchaseMethodCode"), "name": m.get("purchaseMethodName"),
                    "electronic": str(m.get("digitalPurchase")).lower() == "true",
                    "conditions": m.get("conditionsOfUse")} for m in _list(d.get("purchaseMethod"))]
        try:
            version = int(d.get("version"))
        except (TypeError, ValueError):
            version = 0
        out.append({
            "guid": d.get("guid"), "registration_number": str(d.get("registrationNumber") or "").strip(),
            "version": version, "name": (d.get("name") or "").strip(), "url_eis": d.get("urlEIS"),
            "approve_date": _date(d.get("approveDate")), "use_for_base_date": _date(d.get("useForBaseDate")),
            "place_date": d.get("placeDate"), "modification_reason": (d.get("modificationReason") or "").strip() or None,
            "creator": {"inn": creator.get("inn"), "kpp": creator.get("kpp"), "name": creator.get("fullName"),
                        "short_name": creator.get("shortName")},
            "region": creator.get("region"), "purchase_methods": methods, "customers": customers, "files": files,
            "data": {k: d.get(k) for k in ("contractSignPeriod", "purchaseOrder", "smbParticipationSpecifics",
                                           "competitiveIsElectronic", "paymentPeriod")},
            "xml_sha256": sha,
        })
    if not out:
        raise ValueError("в typalOrderClause нет данных положения")
    return out


def position_for(files: Sequence[Dict[str, Any]], agency_kind: Optional[str]) -> Optional[Dict[str, Any]]:
    """Файл положения для типа учреждения заказчика.

    Если положение одно — оно; если несколько — то, чей тип учреждения совпадает с типом заказчика;
    иначе ``None`` (неоднозначно).
    """
    pos = [f for f in files if f.get("kind") == "position"]
    if len(pos) == 1:
        return pos[0]
    same = [f for f in pos if agency_kind and f.get("agency_kind") == agency_kind]
    return same[0] if len(same) == 1 else None


def _pieces(body: str, limit: int) -> List[str]:
    """Абзацы пункта; абзац длиннее ``limit`` режется по словам."""
    out: List[str] = []
    for para in re.split(r"\n\s*\n|\n", body):
        while len(para) > limit:
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(para[:cut])
            para = para[cut:].lstrip()
        out.append(para)
    return out


def split_points(text: str, limit: int = CHUNK_CHARS) -> List[Dict[str, Any]]:
    """Нарезка положения по пунктам («9.3.», «10.1.»): чанк — один или несколько соседних пунктов до ``limit``.

    Длинный пункт режется по абзацам. У чанка — номер первого пункта (``point``), чтобы ссылаться на него.

    Args:
        text: Текст положения.
        limit: Наибольшая длина чанка, символов.

    Returns:
        List[Dict[str, Any]]: ``point``, ``text``.
    """
    text = (text or "").replace("\r\n", "\n")
    marks = [(m.start(), m.group(1)) for m in POINT_RE.finditer(text)]
    if not marks:
        marks = [(0, None)]
    elif marks[0][0] > 0:
        marks.insert(0, (0, None))
    pieces = []
    for i, (start, point) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if body:
            pieces.append((point, body))
    chunks: List[Dict[str, Any]] = []
    cur_point, cur = None, ""
    for point, body in pieces:
        if len(body) > limit:
            if cur:
                chunks.append({"point": cur_point, "text": cur})
                cur_point, cur = None, ""
            part = ""
            for para in _pieces(body, limit):
                if part and len(part) + len(para) + 1 > limit:
                    chunks.append({"point": point, "text": part})
                    part = ""
                part = (part + "\n" + para).strip()
            if part:
                chunks.append({"point": point, "text": part})
            continue
        if cur and len(cur) + len(body) + 1 > limit:
            chunks.append({"point": cur_point, "text": cur})
            cur_point, cur = None, ""
        if not cur:
            cur_point = point
        cur = (cur + "\n" + body).strip()
    if cur:
        chunks.append({"point": cur_point, "text": cur})
    return chunks


# ------------------------------------------------------------------ хранение (Postgres сервиса)
SQL_UPSERT_CLAUSE = """
INSERT INTO rm_typal_clauses (registration_number, version, guid, name, url_eis, approve_date, use_for_base_date,
                              place_date, modification_reason, creator_inn, creator_name, region,
                              purchase_methods, data, xml_sha256)
VALUES ($1, $2, $3, $4, $5, $6::date, $7::date, $8::timestamptz, $9, $10, $11, $12, $13::jsonb, $14::jsonb, $15)
ON CONFLICT (registration_number, version) DO UPDATE SET
    guid = EXCLUDED.guid, name = EXCLUDED.name, url_eis = EXCLUDED.url_eis, approve_date = EXCLUDED.approve_date,
    use_for_base_date = EXCLUDED.use_for_base_date, place_date = EXCLUDED.place_date,
    modification_reason = EXCLUDED.modification_reason, creator_inn = EXCLUDED.creator_inn,
    creator_name = EXCLUDED.creator_name, region = EXCLUDED.region, purchase_methods = EXCLUDED.purchase_methods,
    data = EXCLUDED.data, xml_sha256 = EXCLUDED.xml_sha256, loaded_at = NOW()
RETURNING id
"""

SQL_CUSTOMER_CLAUSE = """
SELECT c.id, c.registration_number, c.version, c.name, c.approve_date, c.use_for_base_date, c.creator_name,
       c.modification_reason, c.url_eis, cu.agency_kind, cu.agency_level, cu.name AS customer_name
FROM rm_typal_clause_customers cu
JOIN rm_typal_clauses c ON c.id = cu.clause_id
WHERE cu.inn = $1 AND ($2::text IS NULL OR cu.kpp = $2 OR cu.kpp = '')
  AND (c.use_for_base_date IS NULL OR c.use_for_base_date <= $3::date)
ORDER BY c.use_for_base_date DESC NULLS LAST, c.version DESC
LIMIT 1
"""


def pg_date(v: Any) -> Any:
    """Дата для параметра ``::date`` asyncpg: строка ``ГГГГ-ММ-ДД…`` → ``date`` (asyncpg не принимает строки —
    «'str' object has no attribute 'toordinal'», загрузка 24.10.2026); пусто или не дата — ``None``."""
    import datetime as _dt

    if v is None or isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def pg_timestamp(v: Any) -> Any:
    """Момент для параметра ``::timestamptz`` asyncpg: ISO-строка (в т. ч. с ``Z``) → ``datetime``; иначе ``None``."""
    import datetime as _dt

    if v is None or isinstance(v, _dt.datetime):
        return v
    text = str(v).strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        d = pg_date(text)
        return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc) if d else None


class TypalRepo:
    """Хранение типовых положений в Postgres сервиса (пул ``AIRepo.pool``)."""

    def __init__(self, pool: Any):
        """Запоминает пул ``asyncpg``."""
        self.pool = pool

    async def save_clause(self, clause: Dict[str, Any]) -> int:
        """Сохраняет версию положения, её заказчиков и файлы (заказчики и файлы версии перезаписываются).

        Args:
            clause: Результат :func:`parse_typal_xml` (одна версия).

        Returns:
            int: ``rm_typal_clauses.id``.
        """
        import json

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                cid = await conn.fetchval(
                    SQL_UPSERT_CLAUSE, clause["registration_number"], clause["version"], clause.get("guid"),
                    clause.get("name"), clause.get("url_eis"), pg_date(clause.get("approve_date")),
                    pg_date(clause.get("use_for_base_date")),
                    pg_timestamp(clause.get("place_date")), clause.get("modification_reason"), clause["creator"].get("inn"),
                    clause["creator"].get("name"), clause.get("region"),
                    json.dumps(clause.get("purchase_methods") or [], ensure_ascii=False),
                    json.dumps(clause.get("data") or {}, ensure_ascii=False), clause.get("xml_sha256"))
                await conn.execute("DELETE FROM rm_typal_clause_customers WHERE clause_id = $1", cid)
                await conn.executemany(
                    "INSERT INTO rm_typal_clause_customers (clause_id, inn, kpp, ogrn, name, agency_kind, agency_level, region) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
                    [(cid, c["inn"], c.get("kpp") or "", c.get("ogrn"), c.get("name"), c.get("agency_kind"),
                      c.get("agency_level"), c.get("region")) for c in clause.get("customers") or []])
                for f in clause.get("files") or []:
                    await conn.execute(
                        "INSERT INTO rm_typal_clause_files (clause_id, file_name, description, url, content_uid, kind, agency_kind) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (clause_id, url) DO UPDATE SET "
                        "file_name = EXCLUDED.file_name, description = EXCLUDED.description, "
                        "content_uid = EXCLUDED.content_uid, kind = EXCLUDED.kind, agency_kind = EXCLUDED.agency_kind",
                        cid, f["file_name"], f.get("description"), f.get("url"), f.get("content_uid"), f["kind"],
                        f.get("agency_kind"))
        return int(cid)

    async def files_to_read(self, clause_id: int) -> List[Dict[str, Any]]:
        """Файлы версии без текста: положения, перечни и приказы (``other`` не читаются).

        Файл с тем же ``content_uid``, уже прочитанный в другой версии, получает тот же документ без скачивания.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE rm_typal_clause_files f SET document_id = p.document_id, status = 'done'
                FROM rm_typal_clause_files p
                WHERE f.clause_id = $1 AND f.document_id IS NULL AND p.document_id IS NOT NULL
                  AND p.content_uid = f.content_uid AND p.id <> f.id
                """, clause_id)
            rows = await conn.fetch(
                "SELECT id, file_name, url, kind, agency_kind FROM rm_typal_clause_files "
                "WHERE clause_id = $1 AND document_id IS NULL AND kind <> 'other' ORDER BY id", clause_id)
        return [dict(r) for r in rows]

    async def set_file_document(self, file_id: int, document_id: Optional[int], status: str,
                                error: Optional[str] = None) -> None:
        """Отмечает результат чтения файла положения."""
        await self.pool.execute("UPDATE rm_typal_clause_files SET document_id = $2, status = $3, error = $4 WHERE id = $1",
                                file_id, document_id, status, error)

    async def save_point_chunks(self, document_id: int, chunks: Sequence[Dict[str, Any]],
                                embeddings: Sequence[Optional[Sequence[float]]], model: str) -> None:
        """Заменяет чанки-пункты документа положения (``chunker = typal_points``)."""
        from configs.ai_repo import _vec

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM ai_document_chunks WHERE document_id = $1 AND chunker = $2",
                                   document_id, CHUNKER)
                await conn.executemany(
                    "INSERT INTO ai_document_chunks (document_id, chunk_no, text, embedding, embedding_model, chunker, heading) "
                    "VALUES ($1, $2, $3, $4::vector, $5, $6, $7)",
                    [(document_id, i, c["text"], _vec(e) if e is not None else None, model, CHUNKER, c.get("point"))
                     for i, (c, e) in enumerate(zip(chunks, embeddings))])

    async def customer_clause(self, inn: str, kpp: Optional[str] = None, on: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Действующая на дату версия типового положения заказчика и его файл-положение.

        Args:
            inn: ИНН заказчика.
            kpp: КПП (``None`` — любой).
            on: Дата ``ГГГГ-ММ-ДД`` (по умолчанию сегодня).

        Returns:
            Optional[Dict[str, Any]]: Реквизиты версии, ``agency_kind`` заказчика, ``files``, ``position``
            (файл-положение для его типа учреждения) или ``None``.
        """
        on = on or date.today().isoformat()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(SQL_CUSTOMER_CLAUSE, str(inn).strip(), (kpp or None), pg_date(on))
            if not row:
                return None
            files = [dict(f) for f in await conn.fetch(
                "SELECT id, file_name, url, kind, agency_kind, document_id, status FROM rm_typal_clause_files "
                "WHERE clause_id = $1 ORDER BY id", row["id"])]
        out = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}
        out["files"] = files
        out["position"] = position_for(files, row["agency_kind"])
        return out

    async def search_points(self, document_id: int, embedding: Sequence[float], model: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Ближайшие пункты положения к запросу (для проверки риска 223-ФЗ по положению заказчика)."""
        from configs.ai_repo import _vec

        rows = await self.pool.fetch(
            "SELECT heading AS point, text, 1 - (embedding <=> $2::vector) AS similarity FROM ai_document_chunks "
            "WHERE document_id = $1 AND chunker = $3 AND embedding_model = $4 AND embedding IS NOT NULL "
            "ORDER BY embedding <=> $2::vector LIMIT $5", document_id, _vec(embedding), CHUNKER, model, limit)
        return [dict(r) for r in rows]

"""Репозиторий хранилища знаний: SQL-операции над таблицами ``pe_*``.

Все функции принимают открытое соединение ``asyncpg`` (или совместимый объект с методами
``execute``/``fetchval``) и не знают о флаге ``KNOWLEDGE_STORE_ENABLED`` — флаг проверяется
выше, в :mod:`knowledge_store.hooks`.
"""
import hashlib
import json
from typing import Any, List, Optional, Sequence

SQL_UPSERT_PROCUREMENT = """
INSERT INTO pe_procurements (expertise_id, law, method, object_code, check_type2, passport)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (expertise_id) DO UPDATE SET
    law = EXCLUDED.law, method = EXCLUDED.method, object_code = EXCLUDED.object_code,
    check_type2 = EXCLUDED.check_type2, passport = EXCLUDED.passport, updated_at = NOW()
"""

SQL_UPSERT_DOCUMENT = """
INSERT INTO pe_documents (expertise_id, doc_code, filename, url, sha256, text_full, pages_count, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() + make_interval(days => $8))
ON CONFLICT (expertise_id, sha256) DO UPDATE SET
    doc_code = EXCLUDED.doc_code, filename = EXCLUDED.filename, url = EXCLUDED.url,
    text_full = EXCLUDED.text_full, pages_count = EXCLUDED.pages_count,
    expires_at = EXCLUDED.expires_at, text_purged_at = NULL
RETURNING id
"""

SQL_SET_EXTRACTION = """
UPDATE pe_documents
SET detected_type = $2, readability = $3::jsonb,
    extraction = COALESCE(extraction, '{}'::jsonb) || $4::jsonb
WHERE id = $1
"""

EMBEDDING_DIM = 1024  # должно совпадать с vector(1024) в миграции 001

SQL_DOCS_FOR_INDEXING = """
SELECT d.id, d.text_full FROM pe_documents d
WHERE d.expertise_id = $1 AND d.text_full IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM pe_chunks c WHERE c.document_id = d.id)
ORDER BY d.id
"""

SQL_DELETE_CHUNKS = "DELETE FROM pe_chunks WHERE document_id = $1"

SQL_INSERT_CHUNK = """
INSERT INTO pe_chunks (document_id, expertise_id, idx, page_from, page_to, text, embedding)
VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
"""

SQL_PURGE_EXPIRED = """
WITH expired AS (
    UPDATE pe_documents SET text_full = NULL, text_purged_at = NOW()
    WHERE expires_at < NOW() AND NOT legal_hold AND text_purged_at IS NULL
    RETURNING id
), removed AS (
    DELETE FROM pe_chunks WHERE document_id IN (SELECT id FROM expired)
)
SELECT count(*) FROM expired
"""

SQL_CLEAR = [
    "DELETE FROM pe_facts WHERE expertise_id = $1",
    "DELETE FROM pe_documents WHERE expertise_id = $1",  # pe_chunks удаляются каскадно
]


def to_json(value: Any) -> Optional[str]:
    """Сериализует значение в JSON-строку для параметров ``::jsonb``.

    Args:
        value: Любой JSON-совместимый объект (даты и прочее приводятся к строке).

    Returns:
        Optional[str]: JSON-строка либо ``None``, если ``value`` равно ``None``.
    """
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def file_sha256(path: str) -> str:
    """Считает SHA-256 файла (блоками, без загрузки целиком в память).

    Args:
        path: Путь к файлу.

    Returns:
        str: Шестнадцатеричная контрольная сумма.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


async def save_procurement(conn, expertise_id: int, law: Optional[str], method: Optional[str],
                           object_code: Optional[int], check_type2: Optional[int], passport: Optional[dict] = None) -> None:
    """Создаёт или обновляет паспорт закупки.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        law: Закон (``44-ФЗ`` / ``223-ФЗ``).
        method: Способ закупки.
        object_code: Код объекта экспертизы (5/6/7).
        check_type2: Код способа/вида проверки.
        passport: Прочие поля паспорта (JSON).
    """
    await conn.execute(SQL_UPSERT_PROCUREMENT, int(expertise_id), law, method,
                       None if object_code is None else int(object_code),
                       None if check_type2 is None else int(check_type2), to_json(passport))


async def upsert_document(conn, expertise_id: int, doc_code: Optional[str], filename: Optional[str], url: Optional[str],
                          sha256: str, text_full: str, pages_count: int, retention_days: int) -> int:
    """Сохраняет документ с полным текстом; повторная загрузка того же файла обновляет запись.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        doc_code: Код типа документа (после нормализации).
        filename: Имя файла.
        url: Исходная ссылка.
        sha256: Контрольная сумма файла.
        text_full: Извлечённый текст.
        pages_count: Число страниц.
        retention_days: Срок хранения текста (дней) — для ``expires_at``.

    Returns:
        int: ``pe_documents.id``.
    """
    return await conn.fetchval(SQL_UPSERT_DOCUMENT, int(expertise_id), doc_code, filename, url, sha256,
                               text_full, int(pages_count), int(retention_days))


async def set_document_extraction(conn, document_id: int, detected_type: Optional[str],
                                  readability: Optional[dict], extraction: Optional[dict]) -> None:
    """Дописывает к документу результат ``TypeDataExtractor``.

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``pe_documents.id``.
        detected_type: Определённый тип документа.
        readability: Блок ``readability`` ответа экстрактора.
        extraction: Весь ответ экстрактора (``type_compliance``, ``readability``, ``raw_data``).
    """
    await conn.execute(SQL_SET_EXTRACTION, int(document_id), detected_type, to_json(readability), to_json(extraction))


async def clear_expertise(conn, expertise_id: int) -> None:
    """Удаляет документы, чанки и факты экспертизы (перед повторным прогоном).

    Паспорт и сводные заключения не удаляются.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
    """
    for sql in SQL_CLEAR:
        await conn.execute(sql, int(expertise_id))


def vector_literal(vec: Sequence[float]) -> str:
    """Формирует текстовый литерал pgvector ``[x1,x2,...]`` для параметра ``$n::vector``.

    Args:
        vec: Вектор эмбеддинга.

    Returns:
        str: Литерал вида ``[0.1,0.2]``.

    Raises:
        ValueError: Если длина вектора не равна ``EMBEDDING_DIM``.
    """
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(f"Размерность эмбеддинга {len(vec)} != {EMBEDDING_DIM}")
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def documents_for_indexing(conn, expertise_id: int) -> List[Any]:
    """Возвращает документы экспертизы, у которых есть текст, но ещё нет чанков.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.

    Returns:
        list: Записи с полями ``id`` и ``text_full``.
    """
    return await conn.fetch(SQL_DOCS_FOR_INDEXING, int(expertise_id))


async def replace_chunks(conn, document_id: int, expertise_id: int, chunks: Sequence[Any], vectors: Sequence[Sequence[float]]) -> int:
    """Заменяет чанки документа новыми (вместе с эмбеддингами).

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``pe_documents.id``.
        expertise_id: ID экспертизы.
        chunks: Объекты :class:`knowledge_store.pages.Chunk`.
        vectors: Эмбеддинги, по одному на чанк (в том же порядке).

    Returns:
        int: Число записанных чанков.

    Raises:
        ValueError: Если число векторов не равно числу чанков или размерность неверна.
    """
    if len(chunks) != len(vectors):
        raise ValueError(f"Чанков {len(chunks)}, эмбеддингов {len(vectors)}")
    rows = [(int(document_id), int(expertise_id), c.idx, c.page_from, c.page_to, c.text, vector_literal(v))
            for c, v in zip(chunks, vectors)]
    await conn.execute(SQL_DELETE_CHUNKS, int(document_id))
    await conn.executemany(SQL_INSERT_CHUNK, rows)
    return len(rows)


async def purge_expired(conn) -> int:
    """Очищает тексты и чанки документов с истёкшим сроком хранения (политика 05.10.2026).

    Документы с ``legal_hold`` не затрагиваются. Факты, цитаты и извлечённые данные остаются.

    Args:
        conn: Соединение ``asyncpg``.

    Returns:
        int: Число очищенных документов.
    """
    return int(await conn.fetchval(SQL_PURGE_EXPIRED) or 0)


SQL_DELETE_FORM_FIELDS = "DELETE FROM pe_form_fields WHERE form_code = $1"

SQL_INSERT_FORM_FIELD = """
INSERT INTO pe_form_fields (form_code, field_key, ordinal, label, section, value_kind, criterion_id, check_level, derived)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

SQL_LIST_FORM_CODES = "SELECT DISTINCT form_code FROM pe_form_fields ORDER BY form_code"

SQL_FORM_FIELDS = (
    "SELECT field_key, ordinal, label, section, value_kind, criterion_id, check_level, derived "
    "FROM pe_form_fields WHERE form_code = $1 ORDER BY ordinal"
)


async def replace_form_fields(conn, form_code: str, fields: Sequence[Any]) -> int:
    """Полностью заменяет поля одной формы в справочнике ``pe_form_fields``.

    Операция идемпотентна: повторный импорт того же Excel даёт тот же результат.
    Вызывающий код должен выполнять её внутри транзакции.

    Args:
        conn: Соединение ``asyncpg``.
        form_code: Код формы (``44fz_competition_obj6``).
        fields: Объекты :class:`knowledge_store.forms.FormField`.

    Returns:
        int: Количество записанных полей.
    """
    await conn.execute(SQL_DELETE_FORM_FIELDS, form_code)
    await conn.executemany(SQL_INSERT_FORM_FIELD, [
        (f.form_code, f.field_key, f.ordinal, f.label, f.section, f.value_kind, None, f.check_level, bool(f.derived))
        for f in fields
    ])
    return len(fields)


async def list_form_codes(conn) -> List[str]:
    """Возвращает коды форм, загруженных в справочник.

    Args:
        conn: Соединение ``asyncpg``.

    Returns:
        list[str]: Отсортированные коды форм.
    """
    return [r["form_code"] for r in await conn.fetch(SQL_LIST_FORM_CODES)]


async def get_form_fields(conn, form_code: str) -> list:
    """Читает поля формы из справочника в порядке вывода.

    Args:
        conn: Соединение ``asyncpg``.
        form_code: Код формы.

    Returns:
        list: Записи ``pe_form_fields`` (``field_key``, ``label``, ``value_kind`` …).
    """
    return list(await conn.fetch(SQL_FORM_FIELDS, form_code))


SQL_MERGE_EXTRACTION = """
UPDATE pe_documents
SET extraction = COALESCE(extraction, '{}'::jsonb) || $2::jsonb
WHERE id = $1
"""

SQL_EIS_DOCUMENTS = """
SELECT id, filename, extraction -> 'eis_notice' AS eis_notice
FROM pe_documents
WHERE expertise_id = $1 AND extraction ? 'eis_notice'
"""

SQL_DELETE_FACTS_BY_SOURCE = "DELETE FROM pe_facts WHERE expertise_id = $1 AND source = $2"

SQL_INSERT_FACT = """
INSERT INTO pe_facts (expertise_id, fact_key, value, document_id, page, quote, confidence, source)
VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8)
"""

SQL_GET_FACTS = """
SELECT fact_key, value, document_id, page, quote, confidence, source
FROM pe_facts WHERE expertise_id = $1 ORDER BY id
"""


async def merge_document_extraction(conn, document_id: int, patch: dict) -> None:
    """Дописывает ключи в ``pe_documents.extraction`` (JSONB-слияние, существующие ключи сохраняются).

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``pe_documents.id``.
        patch: Словарь с добавляемыми/обновляемыми верхнеуровневыми ключами.
    """
    await conn.execute(SQL_MERGE_EXTRACTION, int(document_id), to_json(patch))


async def get_eis_notices(conn, expertise_id: int) -> list:
    """Возвращает документы экспертизы с результатом разбора XML извещения ЕИС.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.

    Returns:
        list: Записи ``id``, ``filename``, ``eis_notice`` (JSON ``{"version", "criteria"}``).
    """
    return list(await conn.fetch(SQL_EIS_DOCUMENTS, int(expertise_id)))


async def replace_facts(conn, expertise_id: int, source: str, facts: Sequence[dict]) -> int:
    """Заменяет факты экспертизы одного источника (``eis_xml`` / ``fact_extractor``).

    Повторный запуск идемпотентен: факты других источников не затрагиваются.
    Цитата обрезается до 500 символов. Вызывать внутри транзакции.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        source: Источник фактов.
        facts: Словари с ключами ``fact_key``, ``value``, ``document_id``, ``page``, ``quote``, ``confidence``.

    Returns:
        int: Количество записанных фактов.
    """
    await conn.execute(SQL_DELETE_FACTS_BY_SOURCE, int(expertise_id), source)
    for fact in facts:
        await conn.execute(
            SQL_INSERT_FACT, int(expertise_id), fact["fact_key"], to_json(fact.get("value")),
            fact.get("document_id"), fact.get("page"), (fact.get("quote") or "")[:500] or None,
            fact.get("confidence"), source)
    return len(facts)


async def get_facts(conn, expertise_id: int) -> list:
    """Читает все факты экспертизы.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.

    Returns:
        list: Записи ``pe_facts``.
    """
    return list(await conn.fetch(SQL_GET_FACTS, int(expertise_id)))

SQL_SEARCH_CHUNKS = """
SELECT c.id AS chunk_id, c.document_id, c.page_from, c.text, d.filename, d.doc_code
FROM pe_chunks c JOIN pe_documents d ON d.id = c.document_id
WHERE c.expertise_id = $1 AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> $2::vector
LIMIT $3
"""

SQL_SET_FORM_CODE = "UPDATE pe_procurements SET form_code = $2, updated_at = NOW() WHERE expertise_id = $1"

SQL_PROCUREMENT = "SELECT law, method, object_code, check_type2, form_code FROM pe_procurements WHERE expertise_id = $1"

SQL_EXPERTISES_WITH_TEXTS = """
SELECT DISTINCT expertise_id FROM pe_documents WHERE text_full IS NOT NULL ORDER BY expertise_id
"""


async def set_form_code(conn, expertise_id: int, form_code: Optional[str]) -> None:
    """Записывает код формы заключения в паспорт закупки.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        form_code: Код формы (``None`` — форма не поддерживается).
    """
    await conn.execute(SQL_SET_FORM_CODE, int(expertise_id), form_code)


async def get_procurement(conn, expertise_id: int) -> Optional[dict]:
    """Читает паспорт закупки (``law``, ``method``, ``object_code``, ``check_type2``, ``form_code``).

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.

    Returns:
        Optional[dict]: Строка паспорта или ``None``.
    """
    rows = await conn.fetch(SQL_PROCUREMENT, int(expertise_id))
    return dict(rows[0]) if rows else None


async def list_expertises_with_texts(conn) -> List[int]:
    """Возвращает ID экспертиз, для которых сохранены полные тексты (кандидаты для backfill).

    Args:
        conn: Соединение ``asyncpg``.

    Returns:
        list[int]: ID экспертиз по возрастанию.
    """
    return [int(r["expertise_id"]) for r in await conn.fetch(SQL_EXPERTISES_WITH_TEXTS)]

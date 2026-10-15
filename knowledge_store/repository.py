"""Репозиторий хранилища знаний: SQL-операции над таблицами ``pe_*``.

Все функции принимают открытое соединение ``asyncpg`` (или совместимый объект с методами
``execute``/``fetchval``) и не знают о флаге ``KNOWLEDGE_STORE_ENABLED`` — флаг проверяется
выше, в :mod:`knowledge_store.hooks`.
"""
import json
from typing import Any, List, Optional, Sequence

from knowledge_store.scope import Scope

SQL_UPSERT_PROCUREMENT = """
INSERT INTO pe_procurements (env, expertise_id, law, method, object_code, check_type2, passport)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
ON CONFLICT (env, expertise_id) DO UPDATE SET
    law = EXCLUDED.law, method = EXCLUDED.method, object_code = EXCLUDED.object_code,
    check_type2 = EXCLUDED.check_type2, passport = EXCLUDED.passport, updated_at = NOW()
"""

SQL_UPSERT_DOCUMENT = """
INSERT INTO pe_documents (env, expertise_id, doc_code, filename, url, sha256, ai_document_id, pages_count, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() + make_interval(days => $9))
ON CONFLICT (env, expertise_id, sha256) DO UPDATE SET
    doc_code = EXCLUDED.doc_code, filename = EXCLUDED.filename, url = EXCLUDED.url,
    ai_document_id = EXCLUDED.ai_document_id, text_full = NULL, pages_count = EXCLUDED.pages_count,
    expires_at = EXCLUDED.expires_at, text_purged_at = NULL
RETURNING id
"""

SQL_SET_EXTRACTION = """
UPDATE pe_documents
SET detected_type = $2, readability = $3::jsonb,
    extraction = COALESCE(extraction, '{}'::jsonb) || $4::jsonb
WHERE id = $1
"""

EMBEDDING_DIM = 1024  # должно совпадать с vector(1024) в миграциях 001/003

# Текст документа экспертизы: общий документ (ai_documents), а для строк до миграции 005 — pe_documents.text_full
TEXT_EXPR = "COALESCE(a.text, d.text_full)"
DOC_JOIN = "pe_documents d LEFT JOIN ai_documents a ON a.id = d.ai_document_id"

# Общие документы экспертизы, у которых ещё нет чанков нужной нарезки и модели эмбеддингов
SQL_DOCS_FOR_INDEXING = """
SELECT DISTINCT a.id, a.text AS text_full FROM pe_documents d JOIN ai_documents a ON a.id = d.ai_document_id
WHERE d.env = $1 AND d.expertise_id = $2 AND a.text IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM ai_document_chunks c
                  WHERE c.document_id = a.id AND c.chunker = $3 AND c.embedding_model = $4)
ORDER BY a.id
"""

SQL_DELETE_CHUNKS = "DELETE FROM ai_document_chunks WHERE document_id = $1 AND chunker = $2 AND embedding_model = $3"

SQL_INSERT_CHUNK = """
INSERT INTO ai_document_chunks (document_id, chunker, embedding_model, chunk_no, page_from, page_to, heading, text, embedding)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector)
"""

# Политика хранения по ссылкам: текст и чанки общего документа удаляются, когда у него не осталось живых ссылок —
# ни ссылок экспертиз (pe_documents: срок не истёк или legal_hold), ни ссылок риск-мониторинга (ai_file_links, см.
# LINK_ALIVE_RM). Документы, изменённые меньше суток назад, не трогаются: ссылка на только что записанный текст
# создаётся следующим запросом. Факты, цитаты, извлечённые данные и кэш анализов остаются.
# Для строк до миграции 005 очищается и pe_documents.text_full.
LINK_ALIVE_PE = "(d.legal_hold OR d.expires_at IS NULL OR d.expires_at >= NOW())"
LINK_ALIVE_RM = "(l.legal_hold OR l.expires_at IS NULL OR l.expires_at >= NOW())"   # срок ссылок РМ — миграция 006

SQL_PURGE_EXPIRED = f"""
WITH expired_links AS (
    UPDATE pe_documents SET text_full = NULL, text_purged_at = NOW()
    WHERE expires_at < NOW() AND NOT legal_hold AND text_purged_at IS NULL AND text_full IS NOT NULL
    RETURNING id
), legacy_chunks AS (
    DELETE FROM pe_chunks WHERE document_id IN (SELECT id FROM expired_links)
), dead AS (
    SELECT a.id FROM ai_documents a
    WHERE a.text IS NOT NULL AND a.updated_at < NOW() - interval '1 day'
      AND NOT EXISTS (SELECT 1 FROM pe_documents d WHERE d.ai_document_id = a.id AND {{LINK_ALIVE_PE}})
      AND NOT EXISTS (SELECT 1 FROM ai_file_links l WHERE l.document_id = a.id AND {{LINK_ALIVE_RM}})
), purged AS (
    UPDATE ai_documents SET text = NULL, text_purged_at = NOW(), updated_at = NOW()
    WHERE id IN (SELECT id FROM dead)
    RETURNING id
), removed AS (
    DELETE FROM ai_document_chunks WHERE document_id IN (SELECT id FROM purged)
)
SELECT (SELECT count(*) FROM purged) + (SELECT count(*) FROM expired_links)
"""

SQL_CLEAR = [
    "DELETE FROM pe_facts WHERE env = $1 AND expertise_id = $2",
    "DELETE FROM pe_documents WHERE env = $1 AND expertise_id = $2",  # только ссылки: общий документ, чанки и кэш остаются
]


def to_json(value: Any) -> Optional[str]:
    """Сериализует значение в JSON-строку для параметров ``::jsonb``.

    Args:
        value: Любой JSON-совместимый объект (даты и прочее приводятся к строке).

    Returns:
        Optional[str]: JSON-строка либо ``None``, если ``value`` равно ``None``.
    """
    from knowledge_store.documents import strip_nul
    return None if value is None else json.dumps(strip_nul(value), ensure_ascii=False, default=str)


def file_sha256(path: str) -> str:
    """Считает SHA-256 файла (блоками, без загрузки целиком в память).

    Args:
        path: Путь к файлу.

    Returns:
        str: Шестнадцатеричная контрольная сумма.
    """
    from knowledge_store.documents import file_sha256 as _sha
    return _sha(path)


async def save_procurement(conn, scope: Scope, law: Optional[str], method: Optional[str],
                           object_code: Optional[int], check_type2: Optional[int], passport: Optional[dict] = None) -> None:
    """Создаёт или обновляет паспорт закупки.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        law: Закон (``44-ФЗ`` / ``223-ФЗ``).
        method: Способ закупки.
        object_code: Код объекта экспертизы (5/6/7).
        check_type2: Код способа/вида проверки.
        passport: Прочие поля паспорта (JSON).
    """
    await conn.execute(SQL_UPSERT_PROCUREMENT, scope.env, scope.expertise_id, law, method,
                       None if object_code is None else int(object_code),
                       None if check_type2 is None else int(check_type2), to_json(passport))


async def upsert_document(conn, scope: Scope, doc_code: Optional[str], filename: Optional[str], url: Optional[str],
                          sha256: str, ai_document_id: int, pages_count: int, retention_days: int) -> int:
    """Сохраняет ссылку экспертизы на общий документ; повторная загрузка того же файла обновляет запись.

    Текст хранится в ``ai_documents`` (:mod:`knowledge_store.documents`), здесь — только ссылка, тип,
    извлечённые данные и срок хранения ссылки.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        doc_code: Код типа документа (после нормализации).
        filename: Имя файла.
        url: Исходная ссылка.
        sha256: Контрольная сумма файла.
        ai_document_id: ``ai_documents.id`` общего документа.
        pages_count: Число страниц.
        retention_days: Срок хранения ссылки (дней) — для ``expires_at``.

    Returns:
        int: ``pe_documents.id``.
    """
    return await conn.fetchval(SQL_UPSERT_DOCUMENT, scope.env, scope.expertise_id, doc_code, filename, url, sha256,
                               int(ai_document_id), int(pages_count), int(retention_days))


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


async def clear_expertise(conn, scope: Scope) -> None:
    """Удаляет документы, чанки и факты экспертизы (перед повторным прогоном).

    Паспорт и сводные заключения не удаляются.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
    """
    for sql in SQL_CLEAR:
        await conn.execute(sql, scope.env, scope.expertise_id)


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


async def documents_for_indexing(conn, scope: Scope, chunker: str, embedding_model: str) -> List[Any]:
    """Возвращает общие документы экспертизы, у которых есть текст, но ещё нет чанков этой нарезки и модели.

    Документ, уже проиндексированный для другой экспертизы, повторно не индексируется.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        chunker: Схема нарезки (``pages_v2``).
        embedding_model: Модель эмбеддингов.

    Returns:
        list: Записи с полями ``id`` (``ai_documents.id``) и ``text_full``.
    """
    return await conn.fetch(SQL_DOCS_FOR_INDEXING, scope.env, scope.expertise_id, chunker, embedding_model)


async def replace_chunks(conn, document_id: int, chunker: str, embedding_model: str, chunks: Sequence[Any],
                         vectors: Sequence[Sequence[float]]) -> int:
    """Заменяет чанки общего документа для схемы нарезки и модели эмбеддингов.

    Чанки других нарезок (например, риск-мониторинга) не затрагиваются.

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``ai_documents.id``.
        chunker: Схема нарезки.
        embedding_model: Модель эмбеддингов.
        chunks: Объекты :class:`knowledge_store.pages.Chunk`.
        vectors: Эмбеддинги, по одному на чанк (в том же порядке).

    Returns:
        int: Число записанных чанков.

    Raises:
        ValueError: Если число векторов не равно числу чанков или размерность неверна.
    """
    if len(chunks) != len(vectors):
        raise ValueError(f"Чанков {len(chunks)}, эмбеддингов {len(vectors)}")
    rows = [(int(document_id), chunker, embedding_model, c.idx, c.page_from, c.page_to, getattr(c, "heading", None),
             c.text, vector_literal(v)) for c, v in zip(chunks, vectors)]
    await conn.execute(SQL_DELETE_CHUNKS, int(document_id), chunker, embedding_model)
    await conn.executemany(SQL_INSERT_CHUNK, rows)
    return len(rows)


def purge_sql() -> str:
    """SQL очистки с текущими условиями «живой» ссылки (подставляются при вызове, чтобы их можно было уточнять).

    Returns:
        str: Текст запроса.
    """
    return SQL_PURGE_EXPIRED.replace("{LINK_ALIVE_PE}", LINK_ALIVE_PE).replace("{LINK_ALIVE_RM}", LINK_ALIVE_RM)


async def purge_expired(conn) -> int:
    """Очищает тексты и чанки документов, на которые не осталось живых ссылок (политика хранения).

    Ссылка экспертизы живёт ``PE_TEXT_RETENTION_DAYS`` после последнего прогона или сводного ЭЗ либо пока
    стоит ``legal_hold``. Факты, цитаты, извлечённые данные и кэш анализов не удаляются.

    Args:
        conn: Соединение ``asyncpg``.

    Returns:
        int: Число очищенных документов (общих и, для строк до миграции 005, ссылок с текстом).
    """
    return int(await conn.fetchval(purge_sql()) or 0)


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
WHERE env = $1 AND expertise_id = $2 AND extraction ? 'eis_notice'
"""

SQL_PRINT_FORM_DOCUMENTS = f"""
SELECT d.id, d.filename, {TEXT_EXPR} AS text_full
FROM {DOC_JOIN}
WHERE d.env = $1 AND d.expertise_id = $2 AND {TEXT_EXPR} IS NOT NULL
  AND left({TEXT_EXPR}, 600) LIKE '%Печатная форма%' AND left({TEXT_EXPR}, 600) LIKE '%Извещение о проведении%'
ORDER BY d.id
"""

SQL_DELETE_FACTS_BY_SOURCE = "DELETE FROM pe_facts WHERE env = $1 AND expertise_id = $2 AND source = $3"

SQL_INSERT_FACT = """
INSERT INTO pe_facts (env, expertise_id, fact_key, value, document_id, page, quote, confidence, source)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
"""

SQL_GET_FACTS = """
SELECT fact_key, value, document_id, page, quote, confidence, source
FROM pe_facts WHERE env = $1 AND expertise_id = $2 ORDER BY id
"""


async def merge_document_extraction(conn, document_id: int, patch: dict) -> None:
    """Дописывает ключи в ``pe_documents.extraction`` (JSONB-слияние, существующие ключи сохраняются).

    Args:
        conn: Соединение ``asyncpg``.
        document_id: ``pe_documents.id``.
        patch: Словарь с добавляемыми/обновляемыми верхнеуровневыми ключами.
    """
    await conn.execute(SQL_MERGE_EXTRACTION, int(document_id), to_json(patch))


async def get_eis_notices(conn, scope: Scope) -> list:
    """Возвращает документы экспертизы с результатом разбора XML извещения ЕИС.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        list: Записи ``id``, ``filename``, ``eis_notice`` (JSON ``{"version", "criteria"}``).
    """
    return list(await conn.fetch(SQL_EIS_DOCUMENTS, scope.env, scope.expertise_id))


async def get_print_form_documents(conn, scope: Scope) -> list:
    """Возвращает документы экспертизы, похожие на печатную форму извещения ЕИС (по заголовку текста).

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        list: Записи ``id``, ``filename``, ``text_full`` (окончательная проверка — ``eis_printform.is_print_form``).
    """
    return list(await conn.fetch(SQL_PRINT_FORM_DOCUMENTS, scope.env, scope.expertise_id))


async def replace_facts(conn, scope: Scope, source: str, facts: Sequence[dict]) -> int:
    """Заменяет факты экспертизы одного источника (``eis_xml`` / ``fact_extractor``).

    Повторный запуск идемпотентен: факты других источников не затрагиваются.
    Цитата обрезается до 500 символов. Вызывать внутри транзакции.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        source: Источник фактов.
        facts: Словари с ключами ``fact_key``, ``value``, ``document_id``, ``page``, ``quote``, ``confidence``.

    Returns:
        int: Количество записанных фактов.
    """
    await conn.execute(SQL_DELETE_FACTS_BY_SOURCE, scope.env, scope.expertise_id, source)
    for fact in facts:
        await conn.execute(
            SQL_INSERT_FACT, scope.env, scope.expertise_id, fact["fact_key"], to_json(fact.get("value")),
            fact.get("document_id"), fact.get("page"), (fact.get("quote") or "")[:500] or None,
            fact.get("confidence"), source)
    return len(facts)


async def get_facts(conn, scope: Scope) -> list:
    """Читает все факты экспертизы.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        list: Записи ``pe_facts``.
    """
    return list(await conn.fetch(SQL_GET_FACTS, scope.env, scope.expertise_id))

# Поиск по чанкам экспертизы: точный (без индекса) перебор её чанков. Общая таблица ai_document_chunks содержит
# чанки всех документов, и HNSW с фильтром по экспертизе отбрасывал бы соседей после поиска — фрагменты терялись бы.
# MATERIALIZED не даёт планировщику применить индекс к отфильтрованному набору. document_id — pe_documents.id.
SQL_SEARCH_CHUNKS = """
WITH cand AS MATERIALIZED (
    SELECT c.id AS chunk_id, d.id AS document_id, c.page_from, c.heading, c.text, d.filename, d.doc_code, c.embedding
    FROM pe_documents d JOIN ai_document_chunks c ON c.document_id = d.ai_document_id
    WHERE d.env = $1 AND d.expertise_id = $2 AND c.chunker = $5 AND c.embedding_model = $6 AND c.embedding IS NOT NULL
)
SELECT chunk_id, document_id, page_from, heading, text, filename, doc_code FROM cand
ORDER BY embedding <=> $3::vector, chunk_id
LIMIT $4
"""

SQL_SEARCH_NOTICE_CHUNKS = """
WITH cand AS MATERIALIZED (
    SELECT c.id AS chunk_id, d.id AS document_id, c.page_from, c.heading, c.text, d.filename, d.doc_code, c.embedding
    FROM pe_documents d JOIN ai_document_chunks c ON c.document_id = d.ai_document_id
    WHERE d.env = $1 AND d.expertise_id = $2 AND c.chunker = $5 AND c.embedding_model = $6 AND c.embedding IS NOT NULL
      AND d.doc_code = ANY($7::text[])
)
SELECT chunk_id, document_id, page_from, heading, text, filename, doc_code FROM cand
ORDER BY embedding <=> $3::vector, chunk_id
LIMIT $4
"""

SQL_SET_FORM_CODE = "UPDATE pe_procurements SET form_code = $3, updated_at = NOW() WHERE env = $1 AND expertise_id = $2"

SQL_PROCUREMENT = ("SELECT law, method, object_code, check_type2, form_code, nmck, advance, funding, passport "
                    "FROM pe_procurements WHERE env = $1 AND expertise_id = $2")

SQL_EXPERTISES_WITH_TEXTS = f"""
SELECT DISTINCT d.env, d.expertise_id FROM {DOC_JOIN} WHERE {TEXT_EXPR} IS NOT NULL ORDER BY d.env, d.expertise_id
"""


async def set_form_code(conn, scope: Scope, form_code: Optional[str]) -> None:
    """Записывает код формы заключения в паспорт закупки.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        form_code: Код формы (``None`` — форма не поддерживается).
    """
    await conn.execute(SQL_SET_FORM_CODE, scope.env, scope.expertise_id, form_code)


async def get_procurement(conn, scope: Scope) -> Optional[dict]:
    """Читает паспорт закупки (``law``, ``method``, ``object_code``, ``check_type2``, ``form_code``).

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        Optional[dict]: Строка паспорта или ``None``.
    """
    rows = await conn.fetch(SQL_PROCUREMENT, scope.env, scope.expertise_id)
    return dict(rows[0]) if rows else None


async def list_expertises_with_texts(conn, env: Optional[str] = None) -> List[Scope]:
    """Возвращает экспертизы, для которых сохранены полные тексты (кандидаты для backfill).

    Args:
        conn: Соединение ``asyncpg``.
        env: Только это окружение (``None`` — все).

    Returns:
        list[Scope]: Экспертизы по окружению и возрастанию ID.
    """
    rows = [Scope(r["env"], int(r["expertise_id"])) for r in await conn.fetch(SQL_EXPERTISES_WITH_TEXTS)]
    return [s for s in rows if env is None or s.env == env]


SQL_DOCUMENTS_BRIEF = f"""
SELECT d.id, d.doc_code, d.filename, {TEXT_EXPR} AS text_full, COALESCE(a.text_purged_at, d.text_purged_at) AS text_purged_at,
       d.extraction, ({TEXT_EXPR} IS NOT NULL) AS has_text
FROM {DOC_JOIN} WHERE d.env = $1 AND d.expertise_id = $2 ORDER BY d.id
"""
SQL_INSERT_SUMMARY = """
INSERT INTO pe_summary_opinions (env, expertise_id, form_code, data, trace, status)
VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6) RETURNING id
"""
SQL_TOUCH_EXPIRY = """
UPDATE pe_documents SET expires_at = NOW() + make_interval(days => $3)
WHERE env = $1 AND expertise_id = $2 AND text_purged_at IS NULL AND NOT legal_hold
"""


async def get_documents_brief(conn, scope: Scope) -> list:
    """Читает документы экспертизы для сводного ЭЗ (с полным текстом, если он ещё не очищен).

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.

    Returns:
        list: Записи ``id, doc_code, filename, text_full, text_purged_at, extraction, has_text``.
    """
    return list(await conn.fetch(SQL_DOCUMENTS_BRIEF, scope.env, scope.expertise_id))


async def insert_summary(conn, scope: Scope, form_code: str, data: dict, trace: dict, status: str) -> int:
    """Сохраняет сводное ЭЗ в ``pe_summary_opinions``.

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        form_code: Код формы.
        data: JSON ровно с ключами формы.
        trace: Доказательства и пояснения по полям (отдельно от ``data``).
        status: ``draft`` (все поля решены) или ``needs_review`` (есть поля для эксперта).

    Returns:
        int: ``pe_summary_opinions.id``.
    """
    return int(await conn.fetchval(SQL_INSERT_SUMMARY, scope.env, scope.expertise_id, form_code, to_json(data), to_json(trace), status))


async def touch_expiry(conn, scope: Scope, days: int) -> None:
    """Продлевает срок хранения текстов экспертизы (политика 2а.1: при повторной генерации).

    Args:
        conn: Соединение ``asyncpg``.
        scope: Экспертиза в окружении.
        days: Срок хранения в днях от текущего момента.
    """
    await conn.execute(SQL_TOUCH_EXPIRY, scope.env, scope.expertise_id, int(days))

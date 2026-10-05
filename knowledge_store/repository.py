"""Репозиторий хранилища знаний: SQL-операции над таблицами ``pe_*``.

Все функции принимают открытое соединение ``asyncpg`` (или совместимый объект с методами
``execute``/``fetchval``) и не знают о флаге ``KNOWLEDGE_STORE_ENABLED`` — флаг проверяется
выше, в :mod:`knowledge_store.hooks`.
"""
import hashlib
import json
from typing import Any, Optional

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
SET detected_type = $2, readability = $3::jsonb, extraction = $4::jsonb
WHERE id = $1
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

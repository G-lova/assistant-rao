"""Тестовая схема Postgres сервиса: все миграции ``migrations/NNN_*.sql`` без pgvector.

В песочнице нет расширения pgvector, поэтому:

* ``CREATE EXTENSION`` и HNSW-индексы убираются, ``vector(N)`` заменяется на ``real[]``;
* оператор ``<=>`` (косинусное расстояние) определяется для ``real[]`` SQL-функцией — запросы поиска
  выполняются настоящим Postgres с той же семантикой;
* литералы ``$n::vector`` переводятся в массивы в :class:`tests.pg_psql.PsqlConn`.

Колонкам ``env`` в тестовой схеме задано значение по умолчанию ``dev`` — только для удобства ручных
``INSERT`` в тестах; код сервиса всегда передаёт окружение явно.
"""
import re
from pathlib import Path

from scripts.apply_migrations import list_migrations, render_sql

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
TEST_ENV = "dev"

DROP = """
DROP TABLE IF EXISTS expert_publications, expert_pub_sources, expert_pub_experts, raw_document_data, procurement_identity, pe_summary_opinions, pe_facts, pe_chunks, pe_documents, pe_form_fields,
    pe_procurements, ai_file_links, ai_document_analysis, ai_document_chunks, ai_documents, schema_migrations CASCADE;
"""

COSINE = """
CREATE OR REPLACE FUNCTION test_cosine_distance(a real[], b real[]) RETURNS double precision AS $$
    SELECT 1 - (SELECT sum(x * y) FROM unnest(a, b) AS t(x, y))
             / NULLIF(sqrt((SELECT sum(x * x) FROM unnest(a) AS t(x))) * sqrt((SELECT sum(y * y) FROM unnest(b) AS t(y))), 0)
$$ LANGUAGE sql IMMUTABLE;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_operator WHERE oprname = '<=>' AND oprleft = 'real[]'::regtype) THEN
        CREATE OPERATOR <=> (LEFTARG = real[], RIGHTARG = real[], FUNCTION = test_cosine_distance);
    END IF;
END $$;
"""


def shim(sql: str) -> str:
    """Готовит текст миграции для Postgres без pgvector.

    Args:
        sql: Исходный SQL миграции.

    Returns:
        str: SQL без расширения и HNSW-индексов, с ``real[]`` вместо ``vector(N)``.
    """
    sql = render_sql(sql, 1024, TEST_ENV)
    sql = re.sub(r"CREATE EXTENSION[^;]*;", "", sql)
    sql = re.sub(r"CREATE INDEX[^;]*USING hnsw[^;]*;", "", sql, flags=re.S)
    return re.sub(r"vector\(\d+\)", "real[]", sql)


def build(conn) -> None:
    """Пересоздаёт тестовую схему: все миграции по порядку плюс оператор ``<=>`` для ``real[]``.

    Args:
        conn: :class:`tests.pg_psql.PsqlConn`.
    """
    conn._run(DROP)
    conn._run(COSINE)
    for path in list_migrations(MIGRATIONS):
        conn._run(shim(path.read_text(encoding="utf-8")))
    conn._run("""
    DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT table_name FROM information_schema.columns
                 WHERE column_name = 'env' AND table_schema = 'public' AND table_name LIKE 'pe\\_%'
        LOOP EXECUTE format('ALTER TABLE %I ALTER COLUMN env SET DEFAULT %L', r.table_name, 'dev'); END LOOP;
    END $$;""")

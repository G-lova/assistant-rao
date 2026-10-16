-- 010: типовые положения о закупке (223-ФЗ) из ЕИС (typalOrderClause), решение 16.10.2026.
-- Каждая версия положения хранится отдельно (новая редакция не затирает старую): риск закупки проверяется по
-- редакции, действовавшей на её дату. Заказчики версии — по ИНН/КПП из customerInfo, файлы — положения
-- (по типу учреждения), перечни, приказы; текст файла — общий документ ai_documents, чанки — по пунктам
-- (ai_document_chunks.chunker = 'typal_points'). Данные ЕИС общие для всех окружений. Операторы идемпотентны.

CREATE TABLE IF NOT EXISTS rm_typal_clauses (
    id                  BIGSERIAL PRIMARY KEY,
    registration_number TEXT NOT NULL,
    version             INT  NOT NULL,
    guid                TEXT,
    name                TEXT,
    url_eis             TEXT,
    approve_date        DATE,
    use_for_base_date   DATE,          -- с какой даты применяется редакция
    place_date          TIMESTAMPTZ,
    modification_reason TEXT,          -- приказ об изменениях
    creator_inn         TEXT,
    creator_name        TEXT,
    region              TEXT,
    purchase_methods    JSONB,
    data                JSONB,
    xml_sha256          TEXT,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (registration_number, version)
);

CREATE TABLE IF NOT EXISTS rm_typal_clause_customers (
    clause_id    BIGINT NOT NULL REFERENCES rm_typal_clauses(id) ON DELETE CASCADE,
    inn          TEXT NOT NULL,
    kpp          TEXT NOT NULL DEFAULT '',
    ogrn         TEXT,
    name         TEXT,
    agency_kind  TEXT,                 -- budget | autonomous | treasury | unitary
    agency_level TEXT,                 -- SUBJECT | MUNICIPAL | STATE
    region       TEXT,
    PRIMARY KEY (clause_id, inn, kpp)
);
CREATE INDEX IF NOT EXISTS ix_rm_typal_customers_inn ON rm_typal_clause_customers (inn);

CREATE TABLE IF NOT EXISTS rm_typal_clause_files (
    id           BIGSERIAL PRIMARY KEY,
    clause_id    BIGINT NOT NULL REFERENCES rm_typal_clauses(id) ON DELETE CASCADE,
    file_name    TEXT,
    description  TEXT,
    url          TEXT NOT NULL,
    content_uid  TEXT,                 -- одинаковый файл в разных версиях читается один раз
    kind         TEXT,                 -- position | list | order | other
    agency_kind  TEXT,                 -- для положения: тип учреждения (ГАУ, ГБУ …)
    document_id  BIGINT REFERENCES ai_documents(id),
    status       TEXT,                 -- done | failed | no_text
    error        TEXT,
    UNIQUE (clause_id, url)
);
CREATE INDEX IF NOT EXISTS ix_rm_typal_files_uid ON rm_typal_clause_files (content_uid);

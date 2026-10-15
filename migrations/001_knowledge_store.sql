-- 001: хранилище знаний «РАО Эксперт» (только НОВЫЕ таблицы pe_*; raw_document_data не затрагивается).
-- Файл можно выполнять напрямую через psql. Размерность эмбеддинга 1024 (Qwen3-Embedding-0.6B);
-- для другой модели измените vector(1024) ниже ДО первого применения.
-- Откат: DROP TABLE pe_summary_opinions, pe_facts, pe_chunks, pe_documents, pe_form_fields, pe_procurements CASCADE;

CREATE EXTENSION IF NOT EXISTS vector;

-- Паспорт закупки (один на экспертизу)
CREATE TABLE IF NOT EXISTS pe_procurements (
    expertise_id   INTEGER PRIMARY KEY,
    law            TEXT,            -- '44-ФЗ' / '223-ФЗ'
    method         TEXT,            -- Конкурс / Аукцион / ...
    object_code    INTEGER,         -- expertises.object (5/6/7)
    check_type2    INTEGER,
    form_code      TEXT,            -- ключ формы из pe_form_fields, напр. 44fz_competition_obj6
    nmck           NUMERIC,
    advance        NUMERIC,
    funding        TEXT,
    passport       JSONB,           -- прочие поля паспорта
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Документы экспертизы с полным текстом
CREATE TABLE IF NOT EXISTS pe_documents (
    id               BIGSERIAL PRIMARY KEY,
    expertise_id     INTEGER NOT NULL,
    doc_code         TEXT,           -- код после normalize_doc_codes
    detected_type    TEXT,
    filename         TEXT,
    url              TEXT,
    sha256           TEXT NOT NULL,
    text_full        TEXT,           -- обнуляется политикой хранения
    pages_count      INTEGER,
    readability      JSONB,
    extraction       JSONB,          -- результат TypeDataExtractor
    expires_at       TIMESTAMPTZ,    -- политика: now() + PE_TEXT_RETENTION_DAYS
    legal_hold       BOOLEAN NOT NULL DEFAULT FALSE,
    text_purged_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (expertise_id, sha256)
);
CREATE INDEX IF NOT EXISTS ix_pe_documents_expertise ON pe_documents (expertise_id);
CREATE INDEX IF NOT EXISTS ix_pe_documents_expires ON pe_documents (expires_at) WHERE text_purged_at IS NULL;

-- Чанки с эмбеддингами
CREATE TABLE IF NOT EXISTS pe_chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  BIGINT NOT NULL REFERENCES pe_documents(id) ON DELETE CASCADE,
    expertise_id INTEGER NOT NULL,
    idx          INTEGER NOT NULL,
    page_from    INTEGER,
    page_to      INTEGER,
    text         TEXT NOT NULL,
    embedding    vector(1024),
    UNIQUE (document_id, idx)
);
CREATE INDEX IF NOT EXISTS ix_pe_chunks_expertise ON pe_chunks (expertise_id);
CREATE INDEX IF NOT EXISTS ix_pe_chunks_embedding ON pe_chunks USING hnsw (embedding vector_cosine_ops);

-- Факты с доказательствами
CREATE TABLE IF NOT EXISTS pe_facts (
    id           BIGSERIAL PRIMARY KEY,
    expertise_id INTEGER NOT NULL,
    fact_key     TEXT NOT NULL,
    value        JSONB,
    document_id  BIGINT REFERENCES pe_documents(id) ON DELETE SET NULL,
    page         INTEGER,
    quote        TEXT,               -- <= 500 символов
    confidence   REAL,
    source       TEXT,               -- type_data_extractor / fact_extractor / eis_xml
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_pe_facts_expertise_key ON pe_facts (expertise_id, fact_key);

-- Справочник полей форм заключения (из «Отчеты и закупки поля.xlsx»)
CREATE TABLE IF NOT EXISTS pe_form_fields (
    form_code    TEXT NOT NULL,
    field_key    TEXT NOT NULL,      -- field2_1_1 и т.д.
    ordinal      INTEGER NOT NULL,
    label        TEXT NOT NULL,
    section      TEXT,
    value_kind   TEXT NOT NULL,      -- presence (1/0/2) | compliance | bool | text | number | meta
    criterion_id TEXT,
    check_level  INTEGER,            -- 1 код / 2 ИИ / 3 эксперт
    PRIMARY KEY (form_code, field_key)
);

-- Сводные экспертные заключения
CREATE TABLE IF NOT EXISTS pe_summary_opinions (
    id           BIGSERIAL PRIMARY KEY,
    expertise_id INTEGER NOT NULL,
    form_code    TEXT NOT NULL,
    data         JSONB NOT NULL,     -- ровно ключи формы, как expertise_expert_opinion.data
    trace        JSONB,              -- evidence/reasoning по полям
    status       TEXT NOT NULL DEFAULT 'draft',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_pe_summary_expertise ON pe_summary_opinions (expertise_id, created_at DESC);

-- Хранилище анализа документов риск-мониторинга (ai_*). На сервере применена как 001_ai_analysis.sql;
-- в репозитории — под свободным номером 003. Все операторы идемпотентны (IF NOT EXISTS).
-- Применять вручную / миграцией (init.sql выполняется только на пустом volume).
-- Требуется образ pgvector/pgvector:pg15 вместо postgres:15-alpine.

CREATE EXTENSION IF NOT EXISTS vector;

-- Уникальный документ (по содержимому файла)
CREATE TABLE IF NOT EXISTS ai_documents (
    id             BIGSERIAL PRIMARY KEY,
    content_sha256 CHAR(64) NOT NULL UNIQUE,
    text_sha256    CHAR(64),
    file_ext       TEXT,
    size_bytes     BIGINT,
    text           TEXT,
    ocr_status     TEXT NOT NULL DEFAULT 'pending',   -- pending / done / failed
    ocr_error      TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Чанки + эмбеддинги (Qwen3-Embedding-0.6B -> 1024)
CREATE TABLE IF NOT EXISTS ai_document_chunks (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES ai_documents(id) ON DELETE CASCADE,
    chunk_no        INT NOT NULL,
    text            TEXT NOT NULL,
    embedding       vector(1024),
    embedding_model TEXT NOT NULL,
    UNIQUE (document_id, chunk_no, embedding_model)
);
CREATE INDEX IF NOT EXISTS idx_ai_chunks_embedding
    ON ai_document_chunks USING hnsw (embedding vector_cosine_ops);

-- Результаты анализа, версионируются промптом и моделью
CREATE TABLE IF NOT EXISTS ai_document_analysis (
    id             BIGSERIAL PRIMARY KEY,
    document_id    BIGINT NOT NULL REFERENCES ai_documents(id) ON DELETE CASCADE,
    analysis_type  TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    result         JSONB NOT NULL,      -- полный результат с evidence
    compact        JSONB,               -- то, что уходит во внешнюю БД
    confidence     REAL,
    status         TEXT NOT NULL DEFAULT 'done',
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (document_id, analysis_type, prompt_version, model)
);

-- Связь внешнего файла (risk_monitoring_files.id) с документом, по окружениям
CREATE TABLE IF NOT EXISTS ai_file_links (
    env              TEXT NOT NULL,
    external_file_id BIGINT NOT NULL,
    document_id      BIGINT NOT NULL REFERENCES ai_documents(id),
    linked_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (env, external_file_id)
);

-- 011: публикации и монографии экспертов для подбора экспертов (similarity по публикациям), решение 31.10.2026.
-- Источники — файлы анкеты эксперта (media: documentPublication / documentMonographs) и ссылки из анкеты
-- (users.linkPublication / linkMonographs). Внутренняя БД хранит снимок источников и сравнивает его с внешней БД
-- при каждой синхронизации: новые источники ставятся в очередь, исчезнувшие помечаются removed, неизменённые не
-- трогаются. Текст файла — общий документ ai_documents (по SHA-256: тот же файл у другого эксперта или повторно
-- загруженный не читается и не разбирается заново), разбор файла — кэш ai_document_analysis
-- (analysis_type = 'expert_publications'). Операторы идемпотентны.

-- Эксперт: имя для проверки авторства и отпечаток источников (изменение анкеты видно без сравнения всех строк)
CREATE TABLE IF NOT EXISTS expert_pub_experts (
    env            TEXT   NOT NULL,
    expert_id      BIGINT NOT NULL,
    name           TEXT,
    sources_hash   TEXT,                  -- хэш множества источников эксперта на момент синхронизации
    deleted_at     TIMESTAMPTZ,           -- эксперт удалён / перестал быть экспертом во внешней БД
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (env, expert_id)
);

-- Источник публикаций: файл media или ссылка анкеты
CREATE TABLE IF NOT EXISTS expert_pub_sources (
    id            BIGSERIAL PRIMARY KEY,
    env           TEXT   NOT NULL,
    expert_id     BIGINT NOT NULL,
    source_kind   TEXT   NOT NULL,        -- media | link
    source_key    TEXT   NOT NULL,        -- media.id | нормализованный URL
    collection    TEXT   NOT NULL,        -- publication | monograph
    url           TEXT,                   -- адрес скачивания файла / нормализованная ссылка
    file_name     TEXT,
    link_type     TEXT,                   -- media | doi | file | cloud | journal | elibrary | scopus | profile | library | other
    status        TEXT   NOT NULL DEFAULT 'pending',  -- pending | done | empty | failed | unsupported | removed
    error         TEXT,
    attempts      INT    NOT NULL DEFAULT 0,
    document_id   BIGINT REFERENCES ai_documents(id),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at  TIMESTAMPTZ,
    removed_at    TIMESTAMPTZ,
    UNIQUE (env, expert_id, source_kind, source_key)
);
CREATE INDEX IF NOT EXISTS ix_expert_pub_sources_status ON expert_pub_sources (env, status);

-- Публикация, извлечённая из источника (файл со списком трудов даёт много записей)
CREATE TABLE IF NOT EXISTS expert_publications (
    id              BIGSERIAL PRIMARY KEY,
    source_id       BIGINT NOT NULL REFERENCES expert_pub_sources(id) ON DELETE CASCADE,
    env             TEXT   NOT NULL,
    expert_id       BIGINT NOT NULL,
    pub_no          INT    NOT NULL,
    title           TEXT   NOT NULL,
    year            INT,
    venue           TEXT,                 -- журнал / издательство / сборник
    pub_type        TEXT,                 -- article | monograph | textbook | conference | patent | other
    authors         JSONB,                -- список строк; NULL — авторы не указаны
    keywords        TEXT,
    abstract        TEXT,
    doi             TEXT,
    udk             TEXT,
    author_verified BOOLEAN,              -- фамилия эксперта среди авторов; NULL — авторов не определить
    embed_text_sha  CHAR(64),             -- хэш текста эмбеддинга (пересчёт только при изменении)
    embedding       vector({{EMBEDDING_DIM}}),
    embedding_model TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, pub_no)
);
CREATE INDEX IF NOT EXISTS ix_expert_publications_expert ON expert_publications (env, expert_id);

-- 014: нормативные акты (44-ФЗ, 223-ФЗ, НК РФ, постановления Правительства) по редакциям, решение 10.11.2026.
-- Через API ЕИС акты не выгружаются: сводная редакция (PDF / HTML КонсультантПлюс) загружается вручную, каждая
-- загрузка — версия акта с датой редакции (новая редакция не затирает старую: ссылку риска проверяем по редакции,
-- действовавшей на дату закупки). Текст — общий документ ai_documents, нормы — по статьям / частям / пунктам
-- (rm_legal_norms), чанки с эмбеддингами — ai_document_chunks.chunker = 'law_norms'. Данные общие для всех
-- окружений. Операторы идемпотентны.

CREATE TABLE IF NOT EXISTS rm_legal_acts (
    id             BIGSERIAL PRIMARY KEY,
    act_key        TEXT NOT NULL,          -- 44-ФЗ, 223-ФЗ, НК, ПП-1042
    kind           TEXT NOT NULL,          -- law | code | decree
    number         TEXT,
    act_date       DATE,
    title          TEXT,
    edition_date   DATE NOT NULL,          -- дата редакции (с какой даты действует текст)
    edition_source TEXT,                   -- given | stated | amendments | act_date
    source_name    TEXT,                   -- имя загруженного файла
    text_sha256    TEXT,
    document_id    BIGINT REFERENCES ai_documents(id),
    norms_count    INT,
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (act_key, edition_date)
);

CREATE TABLE IF NOT EXISTS rm_legal_norms (
    id         BIGSERIAL PRIMARY KEY,
    act_id     BIGINT NOT NULL REFERENCES rm_legal_acts(id) ON DELETE CASCADE,
    ord        INT    NOT NULL,            -- порядок в тексте
    segment    TEXT,                       -- утверждённый документ постановления («Правила …»); NULL — основной текст
    lvl1       TEXT,                       -- статья (закон, кодекс)
    lvl2       TEXT,                       -- часть (закон) / пункт (кодекс, постановление)
    lvl3       TEXT,                       -- пункт «1)»
    lvl4       TEXT,                       -- подпункт «а)»
    heading    TEXT,                       -- название статьи / раздела
    address    TEXT NOT NULL,              -- «п. 1 ч. 6 ст. 96 44-ФЗ»
    text       TEXT NOT NULL,
    lost_force BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (act_id, ord)
);
CREATE INDEX IF NOT EXISTS ix_rm_legal_norms_key ON rm_legal_norms (act_id, lvl1, lvl2, lvl3, lvl4);

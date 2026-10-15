-- 009: результаты анализа риск-мониторинга во внутренней БД (решение 15.10.2026).
-- ai_analysis файлов и XML-событий хранится и у нас, а не только во внешней БД: если запись во внешнюю БД не
-- удалась или прогон был без записи (push=false), результат не теряется — его можно дозаписать без повторного
-- анализа, а следующий анализ (сравнение редакций, файлы события) берёт его отсюда.
-- Хранится последний результат по (env, kind, external_id). Все операторы идемпотентны.

CREATE TABLE IF NOT EXISTS ai_rm_results (
    env          TEXT NOT NULL,
    kind         TEXT NOT NULL,                 -- file | xml
    external_id  BIGINT NOT NULL,               -- risk_monitoring_files.id | risk_monitoring_eis_xml_sources.id
    ai_analysis  JSONB NOT NULL,
    status       TEXT,                          -- pipeline.status (completed)
    debug        BOOLEAN NOT NULL DEFAULT FALSE,-- результат режима отладки (force) — не дозаписывается сам
    pushed       BOOLEAN NOT NULL DEFAULT FALSE,
    push_error   TEXT,
    analyzed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pushed_at    TIMESTAMPTZ,
    PRIMARY KEY (env, kind, external_id)
);
CREATE INDEX IF NOT EXISTS ix_ai_rm_results_unpushed ON ai_rm_results (env, kind) WHERE NOT pushed;

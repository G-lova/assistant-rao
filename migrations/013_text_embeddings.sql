-- 013: кэш эмбеддингов текстов для подбора экспертов (решение 05.11.2026).
-- Подбор на каждый запрос отправлял в сервис эмбеддингов тексты всех кандидатов (369 текстов — 17 с из 26 с,
-- экспертиза 7228, stage). Текст анкеты эксперта меняется редко, поэтому вектор хранится по SHA-256 текста и модели
-- и считается только для новых и изменённых текстов (search_experts.embedding_cache). Вектор — массив real[], без
-- привязки к размерности модели; сходство считается в Python. Данные не зависят от окружения: один и тот же текст
-- даёт один и тот же вектор. Неиспользуемые 90 дней записи удаляет ночная очистка (purge_expired_texts_task).
-- Операторы идемпотентны.

CREATE TABLE IF NOT EXISTS text_embeddings (
    model        TEXT     NOT NULL,
    text_sha     CHAR(64) NOT NULL,
    embedding    REAL[]   NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (model, text_sha)
);
CREATE INDEX IF NOT EXISTS ix_text_embeddings_last_used ON text_embeddings (last_used_at);

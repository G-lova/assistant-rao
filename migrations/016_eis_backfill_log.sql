-- 016: журнал дозагрузки документов ЕИС за период (положения о закупке за 2026 год, решение 11.11.2026).
-- Одна строка — вид документа × регион × дата. Повторный запуск пропускает выполненные пары, поэтому дозагрузку
-- можно прерывать и продолжать. Операторы идемпотентны.

CREATE TABLE IF NOT EXISTS rm_eis_backfill_log (
    kind      TEXT NOT NULL,           -- typal | customer
    region    TEXT NOT NULL,           -- код региона ФНС
    day       DATE NOT NULL,
    status    TEXT NOT NULL,           -- done | no_data | error
    versions  INT  NOT NULL DEFAULT 0, -- сохранено версий положений
    error     TEXT,
    done_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, region, day)
);

-- 007: сопоставление экспертизы с карточкой закупки риск-мониторинга.
-- Закупки из риск-мониторинга потом становятся экспертизами: по реестровому номеру закупки (ссылка ЕИС
-- экспертизы) или номеру контракта находится карточка risk_monitoring_contracts. Это даёт эксперту находки РМ
-- по документам той же закупки и продлевает хранение её текстов на время экспертизы.

CREATE TABLE IF NOT EXISTS procurement_identity (
    env              TEXT NOT NULL,
    expertise_id     INTEGER NOT NULL,
    eis_number       TEXT,            -- реестровый номер закупки/контракта из ссылки ЕИС экспертизы (linkDocs)
    contract_number  TEXT,            -- номер контракта из карточки экспертизы (expertises.contract)
    rm_card_id       BIGINT,          -- risk_monitoring_contracts.id
    match_method     TEXT,            -- purchase_number | contract_number | none
    candidates       JSONB,           -- все найденные карточки (если больше одной — сопоставление неоднозначно)
    matched_at       TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (env, expertise_id)
);
CREATE INDEX IF NOT EXISTS ix_procurement_identity_card ON procurement_identity (env, rm_card_id);

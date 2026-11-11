-- 015: положения о закупке конкретных заказчиков (223-ФЗ, orderClause подсистемы POZ223), решение 10.11.2026.
-- Хранятся в таблицах типовых положений (010): clause_type = 'customer'; присоединение к положению другой
-- организации — head_registration_number (её положение) и решение в data. Реестровые номера положений ЕИС —
-- единый реестр, поэтому прежняя уникальность (registration_number, version) сохраняется.
-- Применять до выкладки кода (upsert пишет новые колонки). Операторы идемпотентны.

ALTER TABLE rm_typal_clauses ADD COLUMN IF NOT EXISTS clause_type TEXT NOT NULL DEFAULT 'typal';  -- typal | customer
ALTER TABLE rm_typal_clauses ADD COLUMN IF NOT EXISTS head_registration_number TEXT;
CREATE INDEX IF NOT EXISTS ix_rm_typal_clauses_type ON rm_typal_clauses (clause_type);

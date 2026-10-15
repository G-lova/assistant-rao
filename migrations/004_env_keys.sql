-- 004: окружение (X-API-Database) в ключах pe_*.
-- Один Postgres сервиса обслуживает dev / stage / prod, а expertise_id в окружениях пересекаются:
-- без env в ключе прогон экспертизы 123 из dev перезаписывал данные экспертизы 123 из prod.
-- Уже существующие строки относятся к окружению {{PE_LEGACY_ENV}} (переменная PE_LEGACY_ENV раннера, по умолчанию dev).
-- Откат: удалить колонки env и вернуть прежние ключи (см. 001).

ALTER TABLE pe_procurements ADD COLUMN IF NOT EXISTS env TEXT;
UPDATE pe_procurements SET env = '{{PE_LEGACY_ENV}}' WHERE env IS NULL;
ALTER TABLE pe_procurements ALTER COLUMN env SET NOT NULL;
ALTER TABLE pe_procurements DROP CONSTRAINT IF EXISTS pe_procurements_pkey;
ALTER TABLE pe_procurements ADD CONSTRAINT pe_procurements_pkey PRIMARY KEY (env, expertise_id);

ALTER TABLE pe_documents ADD COLUMN IF NOT EXISTS env TEXT;
UPDATE pe_documents SET env = '{{PE_LEGACY_ENV}}' WHERE env IS NULL;
ALTER TABLE pe_documents ALTER COLUMN env SET NOT NULL;
ALTER TABLE pe_documents DROP CONSTRAINT IF EXISTS pe_documents_expertise_id_sha256_key;
ALTER TABLE pe_documents DROP CONSTRAINT IF EXISTS pe_documents_env_expertise_sha_key;
ALTER TABLE pe_documents ADD CONSTRAINT pe_documents_env_expertise_sha_key UNIQUE (env, expertise_id, sha256);
DROP INDEX IF EXISTS ix_pe_documents_expertise;
CREATE INDEX IF NOT EXISTS ix_pe_documents_env_expertise ON pe_documents (env, expertise_id);

ALTER TABLE pe_chunks ADD COLUMN IF NOT EXISTS env TEXT;
UPDATE pe_chunks SET env = '{{PE_LEGACY_ENV}}' WHERE env IS NULL;
ALTER TABLE pe_chunks ALTER COLUMN env SET NOT NULL;
DROP INDEX IF EXISTS ix_pe_chunks_expertise;
CREATE INDEX IF NOT EXISTS ix_pe_chunks_env_expertise ON pe_chunks (env, expertise_id);

ALTER TABLE pe_facts ADD COLUMN IF NOT EXISTS env TEXT;
UPDATE pe_facts SET env = '{{PE_LEGACY_ENV}}' WHERE env IS NULL;
ALTER TABLE pe_facts ALTER COLUMN env SET NOT NULL;
DROP INDEX IF EXISTS ix_pe_facts_expertise_key;
CREATE INDEX IF NOT EXISTS ix_pe_facts_env_expertise_key ON pe_facts (env, expertise_id, fact_key);

ALTER TABLE pe_summary_opinions ADD COLUMN IF NOT EXISTS env TEXT;
UPDATE pe_summary_opinions SET env = '{{PE_LEGACY_ENV}}' WHERE env IS NULL;
ALTER TABLE pe_summary_opinions ALTER COLUMN env SET NOT NULL;
DROP INDEX IF EXISTS ix_pe_summary_expertise;
CREATE INDEX IF NOT EXISTS ix_pe_summary_env_expertise ON pe_summary_opinions (env, expertise_id, created_at DESC);

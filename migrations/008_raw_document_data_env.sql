-- 008: окружение (X-API-Database) в ключе raw_document_data — результата /evaluate-documents.
-- Один Postgres обслуживает dev / stage / prod: без env прогон экспертизы 123 в dev затирал отчёт экспертизы 123
-- в prod, а /get-subject, /get-law, /get-contract-info и заключение РАО могли прочитать отчёт чужого окружения.
-- Существующие строки относятся к окружению {{PE_LEGACY_ENV}}. Дубли по (env, procurement_id) схлопываются: остаётся
-- самая свежая строка. Уникальный ключ нужен для атомарной записи результата (INSERT … ON CONFLICT).
-- Таблицу создаёт init.sql; если её нет (база без init.sql), создаётся с той же структурой.

CREATE TABLE IF NOT EXISTS raw_document_data (
    id SERIAL PRIMARY KEY,
    procurement_id INTEGER NOT NULL,
    acceptance_act JSONB, works_acceptance_doc JSONB, goods_acceptance_doc JSONB, impossible_alternative_doc JSONB,
    penalty_recovery_docs JSONB, warranty_docs JSONB, contract_conditions_docs JSONB, ip_rights_transfer_docs JSONB,
    goods_origin_docs JSONB, additional_materials JSONB, technical_specification JSONB, contract_amendments JSONB,
    description_purchase_object JSONB, notice JSONB, nir_contract JSONB, service_contract JSONB, goods_contract JSONB,
    price_justification_docs JSONB, price_justification JSONB, nir_report JSONB, procurement_policy JSONB,
    bid_evaluation_procedure JSONB, contract_draft JSONB, compliance_certificates JSONB, eis_link JSONB,
    technical_documentation JSONB, goods_invoice JSONB, bid_requirements JSONB, work_results_photos JSONB,
    goods_photos JSONB, contract_execution_expertise JSONB, unknown JSONB, summary_report JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

ALTER TABLE raw_document_data ADD COLUMN IF NOT EXISTS env TEXT;
UPDATE raw_document_data SET env = '{{PE_LEGACY_ENV}}' WHERE env IS NULL;
ALTER TABLE raw_document_data ALTER COLUMN env SET NOT NULL;

DELETE FROM raw_document_data r
USING raw_document_data newer
WHERE newer.env = r.env AND newer.procurement_id = r.procurement_id
  AND (COALESCE(newer.updated_at, newer.created_at, '-infinity'), newer.id)
    > (COALESCE(r.updated_at, r.created_at, '-infinity'), r.id);

ALTER TABLE raw_document_data DROP CONSTRAINT IF EXISTS raw_document_data_env_procurement_key;
ALTER TABLE raw_document_data ADD CONSTRAINT raw_document_data_env_procurement_key UNIQUE (env, procurement_id);

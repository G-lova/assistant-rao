-- Карточки закупок/контрактов для анализа комплекта (ContractAnalyzer).
-- Биндинги: id карточек ({ids}).
SELECT
    c.id,
    c.number,
    c.fz_type,
    c.risk_monitoring_organisation_id AS organisation_id,
    c.date_public,
    c.eis_version,
    c.dataset,
    JSON_EXTRACT(c.ai_analysis, '$.profile.score') AS prev_score,
    JSON_UNQUOTE(JSON_EXTRACT(c.ai_analysis, '$.pipeline.input_hash')) AS prev_hash,
    JSON_UNQUOTE(JSON_EXTRACT(c.ai_analysis, '$.pipeline.status')) AS rm_status
FROM risk_monitoring_contracts c
WHERE c.id IN ({ids})

-- Предыдущие версии файлов: тот же файл (имя и тип владельца) той же закупки с меньшей версией XML ЕИС.
-- Биндинги: storage_path, затем id файлов ({ids}). Из нескольких кандидатов берётся максимальная версия (в коде).
SELECT
    f.id AS current_id,
    p.id,
    p.eis_version,
    p.file_name,
    CASE WHEN p.path IS NOT NULL THEN CONCAT(?, p.path) ELSE p.source_url END AS url,
    JSON_EXTRACT(p.ai_analysis, '$.risks[*].rule_id') AS rule_ids,
    JSON_UNQUOTE(JSON_EXTRACT(p.ai_analysis, '$.content_sha256')) AS content_sha256
FROM risk_monitoring_files f
JOIN risk_monitoring_files p
    ON p.risk_monitoring_contract_id = f.risk_monitoring_contract_id
   AND p.id <> f.id
   AND p.file_name = f.file_name
   AND p.owner_type <=> f.owner_type
   AND COALESCE(p.eis_version, 0) < COALESCE(f.eis_version, 0)
WHERE f.id IN ({ids})

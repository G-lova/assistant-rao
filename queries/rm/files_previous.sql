-- Запасной поиск предыдущей версии файла (если XML-документы не прочитаны): тот же файл
-- (имя и тип владельца) той же закупки с меньшей версией XML ЕИС. Основной путь — diff вложений XML
-- (risk_monitoring/file_versions.py). Биндинги: id файлов ({ids}).
SELECT
    f.id AS current_id,
    p.id,
    p.eis_version,
    p.file_name,
    p.path,
    p.source_url,
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

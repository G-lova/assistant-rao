-- Кандидаты в предыдущую версию файла: по ссылкам из предыдущей версии XML ({urls})
-- и файлы предыдущих XML ({ids}). Биндинги: ссылки, затем id XML.
SELECT
    p.id,
    p.xml_source_id,
    p.eis_version,
    p.file_name,
    p.owner_type,
    p.path,
    p.source_url,
    JSON_EXTRACT(p.ai_analysis, '$.risks[*].rule_id') AS rule_ids,
    JSON_UNQUOTE(JSON_EXTRACT(p.ai_analysis, '$.content_sha256')) AS content_sha256
FROM risk_monitoring_files p
WHERE p.source_url IN ({urls}) OR p.xml_source_id IN ({ids})

-- Файлы XML прогона и всех их закупок (как xml_files.sql, но по закупкам целиком; вложения по ссылкам находятся
-- среди них в коде). Индексы rm_files_xml_source_idx и risk_monitoring_files_risk_monitoring_contract_id_index.
-- Биндинги: id XML ({ids}), id закупок ({cards}).
SELECT
    f.id,
    f.xml_source_id,
    f.risk_monitoring_contract_id AS card_id,
    f.source_url,
    f.file_name,
    f.file_type,
    f.eis_version,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.doc_type.detected_type')) AS detected_type,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.resume')) AS resume,
    JSON_EXTRACT(f.ai_analysis, '$.total_doc_risk') AS total_doc_risk,
    JSON_EXTRACT(f.ai_analysis, '$.risks[*].rule_id') AS rule_ids,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.pipeline.status')) AS rm_status
FROM risk_monitoring_files f
WHERE f.xml_source_id IN ({ids}) OR f.risk_monitoring_contract_id IN ({cards})

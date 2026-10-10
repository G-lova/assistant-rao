-- Файлы, связанные с XML (по xml_source_id) и найденные по ссылкам из XML, с кратким результатом анализа.
-- Биндинги: id XML ({ids}), затем ссылки ({urls}).
SELECT
    f.id,
    f.xml_source_id,
    f.source_url,
    f.file_name,
    f.file_type,
    f.eis_version,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.doc_type.detected_type')) AS detected_type,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.resume')) AS resume,
    JSON_EXTRACT(f.ai_analysis, '$.total_doc_risk') AS total_doc_risk,
    JSON_EXTRACT(f.ai_analysis, '$.risks[*].rule_id') AS rule_ids
FROM risk_monitoring_files f
WHERE f.xml_source_id IN ({ids}) OR f.source_url IN ({urls})

-- Файлы XML за дату выгрузки и всех их закупок (как xml_files.sql, охват — по дате, без списков id и ссылок):
-- привязанные к XML (xml_source_id) и все файлы закупок, которых касаются XML (вложения по ссылкам находятся
-- среди них в коде). Биндинги: дата выгрузки, ещё раз дата.
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
WHERE f.xml_source_id IN (
        SELECT x.id FROM risk_monitoring_eis_xml_sources x
        JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id WHERE a.exact_date = ?)
   OR f.risk_monitoring_contract_id IN (
        SELECT x.risk_monitoring_contract_id FROM risk_monitoring_eis_xml_sources x
        JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id
        WHERE a.exact_date = ? AND x.risk_monitoring_contract_id IS NOT NULL)

-- XML ЕИС за дату выгрузки (S0 ежедневного прогона) со всеми сведениями xml_meta.sql — одним запросом,
-- без списка id (подход xml_processor: охват по дате через JOIN). Биндинги: storage_path, дата выгрузки.
SELECT
    x.id,
    x.risk_monitoring_contract_id AS card_id,
    x.eis_archive_id,
    x.risk_monitoring_contract_id,
    x.xml_source_type,
    x.file_name,
    x.entry_name,
    x.eis_version,
    x.purchase_number,
    x.contract_number,
    x.status,
    CONCAT(?, a.storage_path) AS archive_url,
    a.exact_date,
    a.eis_source,
    a.fz,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.pipeline.status')) AS rm_status,
    JSON_EXTRACT(x.ai_analysis, '$.pipeline.attempt') AS rm_attempt,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.pipeline.llm.deferred')) AS rm_llm_deferred,
    c.number AS card_number,
    c.fz_type AS card_fz,
    c.risk_monitoring_organisation_id AS organisation_id,
    o.monitoring
FROM risk_monitoring_eis_archives a
JOIN risk_monitoring_eis_xml_sources x ON x.eis_archive_id = a.id
LEFT JOIN risk_monitoring_contracts c ON c.id = x.risk_monitoring_contract_id
LEFT JOIN risk_monitoring_organisations o ON o.id = c.risk_monitoring_organisation_id
WHERE a.exact_date = ?
ORDER BY x.id

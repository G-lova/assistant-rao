-- XML-документы ЕИС для анализа событий (XmlEventAnalyzer).
-- Биндинги: storage_path, затем id XML ({ids}).
SELECT
    x.id,
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
    c.number AS card_number,
    c.risk_monitoring_organisation_id AS organisation_id
FROM risk_monitoring_eis_xml_sources x
JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id
LEFT JOIN risk_monitoring_contracts c ON c.id = x.risk_monitoring_contract_id
WHERE x.id IN ({ids})

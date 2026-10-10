-- Метаданные файлов риск-мониторинга и паспорт закупки для анализа (AIFileAnalyzer).
-- Биндинги: storage_path, затем id файлов (плейсхолдеры {ids}).
SELECT
    f.id,
    f.risk_monitoring_contract_id,
    f.xml_source_id,
    f.owner_number,
    f.owner_type,
    f.xml_source_type,
    f.eis_version,
    f.eis_source,
    f.file_type,
    f.file_size,
    f.download_status,
    CASE WHEN f.path IS NOT NULL THEN CONCAT(?, f.path) ELSE f.source_url END AS url,
    CASE WHEN f.file_name LIKE CONCAT('%.', f.file_type) THEN f.file_name
         WHEN f.file_type IS NULL THEN f.file_name
         ELSE CONCAT(f.file_name, '.', f.file_type) END AS file_name,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.pipeline.status')) AS rm_status,
    JSON_EXTRACT(f.ai_analysis, '$.pipeline.attempt') AS rm_attempt,
    c.number AS purchase_number,
    c.fz_type,
    c.risk_monitoring_organisation_id AS organisation_id,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract_details.predmet')) AS purchase_subject,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.vidz')) AS purchase_method,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.nmck')) AS purchase_nmck,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.ispolnenie.contr_zena')) AS contract_price,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract.okpd2_code')) AS okpd2_code,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.owner.zak_name_krat')) AS customer_name,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_name_krat')) AS supplier_name
FROM risk_monitoring_files f
LEFT JOIN risk_monitoring_contracts c ON c.id = f.risk_monitoring_contract_id
WHERE f.id IN ({ids})

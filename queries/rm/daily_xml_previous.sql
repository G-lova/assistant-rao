-- Предыдущие версии всех XML за дату выгрузки (как xml_previous.sql, но охват — по дате, без списка id).
-- Биндинги: storage_path, дата выгрузки. Максимальная версия выбирается в коде.
SELECT
    x.id AS current_id,
    p.id,
    p.entry_name,
    p.eis_version,
    CONCAT(?, pa.storage_path) AS archive_url
FROM risk_monitoring_eis_archives a
JOIN risk_monitoring_eis_xml_sources x ON x.eis_archive_id = a.id
JOIN risk_monitoring_eis_xml_sources p
    ON p.id <> x.id
   AND (
        (x.risk_monitoring_contract_id IS NOT NULL AND p.risk_monitoring_contract_id = x.risk_monitoring_contract_id)
     OR (x.risk_monitoring_contract_id IS NULL AND p.purchase_number <=> x.purchase_number
         AND p.contract_number <=> x.contract_number)
   )
   AND SUBSTRING_INDEX(p.entry_name, '_', 2) = SUBSTRING_INDEX(x.entry_name, '_', 2)
   AND COALESCE(p.eis_version, -1) < COALESCE(x.eis_version, -1)
JOIN risk_monitoring_eis_archives pa ON pa.id = p.eis_archive_id
WHERE a.exact_date = ?

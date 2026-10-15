-- Предыдущие версии XML-документов: тот же документ (тег и номер — первые две части entry_name)
-- той же закупки (или того же номера закупки/контракта для непривязанных XML) с меньшей версией.
-- Биндинги: storage_path, затем id XML ({ids}). Максимальная версия выбирается в коде.
SELECT
    x.id AS current_id,
    p.id,
    p.entry_name,
    p.eis_version,
    CONCAT(?, a.storage_path) AS archive_url
FROM risk_monitoring_eis_xml_sources x
JOIN risk_monitoring_eis_xml_sources p
    ON p.id <> x.id
   AND (
        (x.risk_monitoring_contract_id IS NOT NULL AND p.risk_monitoring_contract_id = x.risk_monitoring_contract_id)
     OR (x.risk_monitoring_contract_id IS NULL AND p.purchase_number <=> x.purchase_number
         AND p.contract_number <=> x.contract_number)
   )
   AND SUBSTRING_INDEX(p.entry_name, '_', 2) = SUBSTRING_INDEX(x.entry_name, '_', 2)
   AND COALESCE(p.eis_version, -1) < COALESCE(x.eis_version, -1)
JOIN risk_monitoring_eis_archives a ON a.id = p.eis_archive_id
WHERE x.id IN ({ids})

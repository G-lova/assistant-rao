-- XML-документы ЕИС по номеру закупки или контракта (для файлов без xml_source_id: поиск XML по owner_number).
-- Биндинги: storage_path, затем номера ({nums}), ещё раз ({nums2}).
SELECT
    x.id,
    x.entry_name,
    x.eis_version,
    x.xml_source_type,
    x.purchase_number,
    x.contract_number,
    x.risk_monitoring_contract_id,
    SUBSTRING_INDEX(x.entry_name, '_', 1) AS tag,
    CONCAT(?, a.storage_path) AS archive_url
FROM risk_monitoring_eis_xml_sources x
JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id
WHERE x.purchase_number IN ({nums}) OR x.contract_number IN ({nums2})

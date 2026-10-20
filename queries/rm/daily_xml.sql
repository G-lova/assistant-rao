-- XML ЕИС за дату выгрузки (S0 ежедневного прогона): id и карточка закупки.
-- Биндинги: дата выгрузки (risk_monitoring_eis_archives.exact_date).
SELECT
    x.id,
    x.risk_monitoring_contract_id AS card_id
FROM risk_monitoring_eis_xml_sources x
JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id
WHERE a.exact_date = ?

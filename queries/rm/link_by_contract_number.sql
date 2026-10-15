-- Привязка по номеру контракта через другие XML того же контракта (индекс rme_xml_contract_idx).
-- Биндинги: номера контрактов ({nums}).
SELECT contract_number, risk_monitoring_contract_id, COUNT(*) AS n
FROM risk_monitoring_eis_xml_sources
WHERE contract_number IN ({nums}) AND risk_monitoring_contract_id IS NOT NULL
GROUP BY contract_number, risk_monitoring_contract_id

-- Регионы (код ФНС из dataset.owner.region) закупок заказчиков на мониторинге: для загрузки типовых положений.
-- Биндингов нет.
SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.owner.region')) AS region
FROM risk_monitoring_contracts c
JOIN risk_monitoring_organisations o ON o.id = c.risk_monitoring_organisation_id
WHERE o.monitoring = 1

-- Закупки и организации заказчиков на мониторинге (ежедневный прогон: кому из событий нужна модель).
-- Без биндингов.
SELECT c.id AS card_id, o.id AS organisation_id
FROM risk_monitoring_organisations o
LEFT JOIN risk_monitoring_contracts c ON c.risk_monitoring_organisation_id = o.id
WHERE o.monitoring = 1 AND o.deleted_at IS NULL

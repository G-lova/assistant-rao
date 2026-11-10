-- ИНН организаций на мониторинге: фильтр загрузки положений о закупке заказчиков (orderClause).
-- Биндингов нет.
SELECT DISTINCT o.inn AS inn
FROM risk_monitoring_organisations o
WHERE o.monitoring = 1 AND o.deleted_at IS NULL AND o.inn IS NOT NULL

-- Контракты заказчиков по поставщикам за период: для риска «любимый поставщик» (BEH-006) на уровне закупки.
-- Биндинги: начало периода (дата), затем id строк организаций-заказчиков ({ids}).
SELECT
    c.risk_monitoring_organisation_id AS organisation_id,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_inn')) AS supplier_inn,
    COUNT(*) AS contracts
FROM risk_monitoring_contracts c
WHERE COALESCE(c.date_public, DATE(c.created_at)) >= ?
  AND c.risk_monitoring_organisation_id IN ({ids})
  AND JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_inn')) IS NOT NULL
GROUP BY c.risk_monitoring_organisation_id, JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_inn'))

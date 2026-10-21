-- Карточки закупок прогона с заказчиком, признаком мониторинга и поставщиком (S2–S4 ежедневного прогона).
-- Биндинги: id карточек ({ids}).
SELECT
    c.id,
    c.number,
    c.risk_monitoring_organisation_id AS organisation_id,
    o.monitoring,
    COALESCE(o.short_name, o.name) AS customer_name,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract_details.predmet')) AS subject,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_inn')) AS supplier_inn,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_kpp')) AS supplier_kpp,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_name_krat')) AS supplier_name,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_name')) AS supplier_full_name
FROM risk_monitoring_contracts c
LEFT JOIN risk_monitoring_organisations o ON o.id = c.risk_monitoring_organisation_id
WHERE c.id IN ({ids})

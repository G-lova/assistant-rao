-- Карточки закупок прогона с заказчиком и признаком мониторинга (S2–S4 ежедневного прогона).
-- Биндинги: id карточек ({ids}).
SELECT
    c.id,
    c.number,
    c.risk_monitoring_organisation_id AS organisation_id,
    o.monitoring,
    COALESCE(o.short_name, o.name) AS customer_name,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract_details.predmet')) AS subject
FROM risk_monitoring_contracts c
LEFT JOIN risk_monitoring_organisations o ON o.id = c.risk_monitoring_organisation_id
WHERE c.id IN ({ids})

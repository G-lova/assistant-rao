-- Все строки организаций с теми же ИНН (по годам и дубли): закупки заказчика собираются по всем строкам с его ИНН/КПП.
-- Биндинги: ИНН ({inns}).
SELECT o.id, o.year, o.inn, o.kpp, o.name, o.short_name, o.monitoring, o.status_id, o.source,
       o.legal_entity_id, o.is_external
FROM risk_monitoring_organisations o
WHERE o.inn IN ({inns}) AND o.deleted_at IS NULL

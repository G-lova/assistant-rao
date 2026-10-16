-- Организации по id (для выбора ключа агрегации и канонической строки).
-- Биндинги: id организаций ({ids}).
SELECT o.id, o.year, o.inn, o.kpp, o.name, o.short_name, o.monitoring, o.status_id, o.source,
       o.legal_entity_id, o.is_external
FROM risk_monitoring_organisations o
WHERE o.id IN ({ids}) AND o.deleted_at IS NULL

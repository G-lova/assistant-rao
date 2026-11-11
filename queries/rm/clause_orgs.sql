-- Головные организации справочника (без филиалов): проверка положений о закупке (223-ФЗ).
-- Биндингов нет.
SELECT o.id, o.inn, o.kpp, COALESCE(NULLIF(o.full_name, ''), o.name) AS name, o.short_name, o.region_raw AS region,
       o.unit_type, o.okogu_name, o.affiliation_name, o.is_actual, o.monitoring
FROM risk_monitoring_organisations o
WHERE o.deleted_at IS NULL AND COALESCE(o.is_filial, 0) = 0 AND o.inn IS NOT NULL AND o.inn <> ''

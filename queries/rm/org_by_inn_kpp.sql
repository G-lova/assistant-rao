-- Организация по ИНН/КПП (все строки: по году и дубли) для выбора канонической строки.
-- Биндинги: ИНН, КПП.
SELECT id, year, monitoring, status_id, source, legal_entity_id
FROM risk_monitoring_organisations
WHERE inn = ? AND kpp = ? AND deleted_at IS NULL

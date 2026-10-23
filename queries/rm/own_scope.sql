-- Организации системы — подведомственные Минобрнауки (не поставщики, is_external = 1): XML, привязкой отнесённые
-- к ним, разбираются моделью. Без биндингов.
SELECT o.id AS organisation_id
FROM risk_monitoring_organisations o
WHERE COALESCE(o.is_external, 0) = 0 AND o.deleted_at IS NULL

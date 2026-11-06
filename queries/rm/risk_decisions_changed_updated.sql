-- Запасной поиск кандидатов на пересчёт после решения — по updated_at строки, если колонки времени решения
-- аналитика (status_changed_at) в окружении нет. Даёт лишние кандидаты: их отсеивает отпечаток решений.
-- Биндинги: минут назад.
SELECT DISTINCT r.risk_monitoring_contract_id AS contract_id
FROM risk_monitoring_document_risks r
WHERE r.updated_at >= NOW() - INTERVAL ? MINUTE
  AND r.risk_monitoring_contract_id IS NOT NULL

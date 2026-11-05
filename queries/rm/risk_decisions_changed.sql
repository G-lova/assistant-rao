-- Закупки, у которых строки рисков менялись за последние N минут (время БД): кандидаты на пересчёт после решения
-- аналитика. Лишние кандидаты отсеиваются в Python по отпечатку решений (pipeline.decisions_hash).
-- Биндинги: минут назад.
SELECT DISTINCT r.risk_monitoring_contract_id AS contract_id
FROM risk_monitoring_document_risks r
WHERE r.updated_at >= NOW() - INTERVAL ? MINUTE
  AND r.risk_monitoring_contract_id IS NOT NULL

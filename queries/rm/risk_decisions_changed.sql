-- Закупки, где аналитик менял статус рисков за последние N минут (время решения аналитика, время БД): кандидаты на
-- пересчёт после решения. updated_at не подходит — меняется и при каждом приёме анализа, и при назначении исполнителя
-- (лишний пересчёт после каждого анализа, 06.11.2026). Лишние кандидаты отсеиваются по отпечатку решений.
-- Биндинги: минут назад.
SELECT DISTINCT r.risk_monitoring_contract_id AS contract_id
FROM risk_monitoring_document_risks r
WHERE r.status_changed_at >= NOW() - INTERVAL ? MINUTE
  AND r.risk_monitoring_contract_id IS NOT NULL

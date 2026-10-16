-- Работа аналитика по рискам файлов: отклонённые (2) и устранённые (4) не учитываются, подтверждённые (1) весят больше.
-- Биндинги: id файлов ({ids}).
SELECT r.risk_monitoring_file_id AS file_id, r.rule_id, r.status
FROM risk_monitoring_document_risks r
WHERE r.risk_monitoring_file_id IN ({ids})

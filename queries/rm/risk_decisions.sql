-- Решения аналитиков по рискам закупок: строки рисков закупки (contract), файлов (file) и событий (xml) со статусом.
-- Дашборды пишут решение по коду во все строки кода; сервис учитывает их в составе рисков и балле
-- (risk_monitoring.risk_decisions, 05.11.2026).
-- Берутся действующие строки и строки с решением аналитика, в том числе снятые: отклонённая строка у дашбордов сразу
-- становится снятой (detection_status = 'cleared'), без этого сервис не видел «Отклонить» и «Не относится» (06.11.2026).
-- Биндинги: id карточек ({ids}).
SELECT r.risk_monitoring_contract_id AS contract_id, r.scope_type, r.scope_id, r.rule_id, r.status
FROM risk_monitoring_document_risks r
WHERE r.risk_monitoring_contract_id IN ({ids})
  AND (r.detection_status IS NULL OR r.detection_status = 'active' OR r.status <> 0)

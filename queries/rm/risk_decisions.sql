-- Решения аналитиков по рискам закупок: строки рисков закупки (contract), файлов (file) и событий (xml) со статусом.
-- Дашборды пишут решение по коду во все строки кода; сервис учитывает их в составе рисков и балле
-- (risk_monitoring.risk_decisions, 05.11.2026). Снятые строки (detection_status = 'cleared') не читаются.
-- Биндинги: id карточек ({ids}).
SELECT r.risk_monitoring_contract_id AS contract_id, r.scope_type, r.scope_id, r.rule_id, r.status
FROM risk_monitoring_document_risks r
WHERE r.risk_monitoring_contract_id IN ({ids})
  AND (r.detection_status IS NULL OR r.detection_status = 'active')

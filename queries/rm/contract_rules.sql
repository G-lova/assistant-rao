-- Рассчитанные показатели правил внешней системы по закупкам (берётся последний расчёт).
-- Биндинги: id карточек ({ids}).
SELECT r.risk_monitoring_contract_id AS card_id, r.`values` AS rule_values, r.calculated_at
FROM risk_monitoring_evaluation_results r
WHERE r.risk_monitoring_contract_id IN ({ids})
ORDER BY r.calculated_at DESC, r.id DESC

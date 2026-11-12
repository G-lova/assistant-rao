-- Закупки с баллом (уже анализировались сервисом): разовый пересчёт паспортов после смены их состава.
-- Биндингов нет.
SELECT c.id FROM risk_monitoring_contracts c WHERE c.rm_score IS NOT NULL ORDER BY c.id

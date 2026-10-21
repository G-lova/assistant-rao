-- Последняя дата выгрузки ЕИС не позже заданной (для запуска прогона по расписанию).
-- Биндинги: дата.
SELECT MAX(a.exact_date) AS exact_date
FROM risk_monitoring_eis_archives a
WHERE a.exact_date <= ?

-- Закупки по 223-ФЗ организаций системы за период: признак приносящей доход деятельности для отчёта о положениях.
-- Биндинги: дата начала, дата конца (не включая).
SELECT c.risk_monitoring_organisation_id AS org_id, COUNT(*) AS purchases,
       MIN(c.date_public) AS first_date, MAX(c.date_public) AS last_date
FROM risk_monitoring_contracts c
WHERE c.fz_type = 223 AND c.date_public >= ? AND c.date_public < ?
GROUP BY c.risk_monitoring_organisation_id

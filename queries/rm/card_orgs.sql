-- Заказчик (организация) карточек закупок: для поиска типовых формулировок разных заказчиков.
-- Биндинги: id карточек ({ids}).
SELECT c.id, c.risk_monitoring_organisation_id AS org_id
FROM risk_monitoring_contracts c
WHERE c.id IN ({ids})

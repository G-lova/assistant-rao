-- Заказчики всех карточек закупок одним запросом на прогон (проверка типовых формулировок): вместо
-- card_orgs.sql на каждый файл с рисками. Карточки, созданные позже, дозапрашиваются через card_orgs.sql.
SELECT c.id, c.risk_monitoring_organisation_id AS org_id
FROM risk_monitoring_contracts c

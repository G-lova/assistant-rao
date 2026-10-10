-- Привязка по ИКЗ (шаг 3): карточки закупок с тем же ИКЗ в dataset. Полный просмотр JSON —
-- до появления индексируемой колонки ikz выполняется только для «сирот». Биндинги: ИКЗ ({nums}).
SELECT id, number, risk_monitoring_organisation_id, date_public,
       JSON_UNQUOTE(JSON_EXTRACT(dataset, '$.izvejenie.ikz')) AS ikz
FROM risk_monitoring_contracts
WHERE JSON_UNQUOTE(JSON_EXTRACT(dataset, '$.izvejenie.ikz')) IN ({nums})

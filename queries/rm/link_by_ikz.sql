-- Привязка по ИКЗ (шаг 3): карточки закупок с тем же ИКЗ. Колонка ikz — вычисляемая из dataset.izvejenie.ikz
-- с индексом rm_contracts_ikz_index (ТЗ 09.10.2026), полного просмотра JSON больше нет. Биндинги: ИКЗ ({nums}).
SELECT id, number, risk_monitoring_organisation_id, date_public, ikz
FROM risk_monitoring_contracts
WHERE ikz IN ({nums})

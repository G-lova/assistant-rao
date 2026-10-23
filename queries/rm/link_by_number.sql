-- Привязка XML без закупки: карточка по номеру закупки (шаг 1 лестницы привязки).
-- Индекс rm_contracts_number_index. Биндинги: номера ({nums}).
SELECT id, number, risk_monitoring_organisation_id, date_public FROM risk_monitoring_contracts WHERE number IN ({nums})

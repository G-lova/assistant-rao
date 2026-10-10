-- Привязка XML без закупки: карточка по номеру закупки (шаг 1 лестницы привязки).
-- Индекса на number нет — запрос выполняется только для «сирот». Биндинги: номера ({nums}).
SELECT id, number, risk_monitoring_organisation_id, date_public FROM risk_monitoring_contracts WHERE number IN ({nums})

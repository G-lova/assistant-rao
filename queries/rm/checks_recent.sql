-- Результаты внеплановых проверок контрольных органов (checkResult без блока complaint) за период: одна проверка
-- охватывает много закупок — связь с закупками и заказчиками по спискам в ai_analysis.facts (check_purchases,
-- check_customer_inns), а не по purchase_number XML. Отбор по закупкам — в Python (risk_monitoring.control_checks).
-- Биндинги: начало периода (дата).
SELECT
    x.id,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.event.event_at')) AS event_at,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.check_number')) AS check_number,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.check_result')) AS check_result,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.check_prescription')) AS check_prescription,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.check_decision_date')) AS check_decision_date,
    JSON_EXTRACT(x.ai_analysis, '$.facts.check_subjects') AS check_subjects,
    JSON_EXTRACT(x.ai_analysis, '$.facts.check_purchases') AS check_purchases,
    JSON_EXTRACT(x.ai_analysis, '$.facts.check_customer_inns') AS check_customer_inns
FROM risk_monitoring_eis_xml_sources x
WHERE x.entry_name LIKE 'checkResult%'
  AND COALESCE(x.created_at, x.updated_at) >= ?
  AND JSON_EXTRACT(x.ai_analysis, '$.facts.check_number') IS NOT NULL

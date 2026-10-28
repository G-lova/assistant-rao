-- Жалобы (события CMP-001) по ИНН заявителя или заказчика: участники процедур скрыты, заявитель раскрывается
-- только в жалобе (сведения — ai_analysis.facts, risk_monitoring.xml_events.complaint_facts).
-- Биндинги: начало периода (дата), ИНН ({inns}), те же ИНН ({inns2}).
SELECT
    x.id,
    x.purchase_number,
    x.risk_monitoring_contract_id,
    SUBSTRING_INDEX(x.entry_name, '_', 1) AS tag,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.event.event_at')) AS event_at,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.complaint_number')) AS complaint_number,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.complainant_inn')) AS complainant_inn,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.complainant_name')) AS complainant_name,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.customer_inn')) AS customer_inn,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.complaint_result')) AS complaint_result
FROM risk_monitoring_eis_xml_sources x
WHERE (x.entry_name LIKE '%omplaint%' OR x.entry_name LIKE 'tenderSuspension%')
  AND COALESCE(x.created_at, x.updated_at) >= ?
  AND JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.event.code')) = 'CMP-001'
  AND (JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.complainant_inn')) IN ({inns})
       OR JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.facts.customer_inn')) IN ({inns2}))

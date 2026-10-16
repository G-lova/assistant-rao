-- XML ЕИС закупок с событием, резюме и рисками (результат анализа событий).
-- Биндинги: id карточек ({ids}).
SELECT
    x.id,
    x.risk_monitoring_contract_id AS card_id,
    SUBSTRING_INDEX(x.entry_name, '_', 1) AS xml_tag,
    x.eis_version,
    x.purchase_number,
    x.contract_number,
    a.exact_date,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.pipeline.status')) AS rm_status,
    JSON_EXTRACT(x.ai_analysis, '$.event') AS event,
    JSON_UNQUOTE(JSON_EXTRACT(x.ai_analysis, '$.resume')) AS resume,
    JSON_EXTRACT(x.ai_analysis, '$.risks') AS risks
FROM risk_monitoring_eis_xml_sources x
JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id
WHERE x.risk_monitoring_contract_id IN ({ids})

-- Файлы закупок с кратким результатом анализа (тип, резюме, риски, суммы, замена новой редакцией).
-- Биндинги: id карточек ({ids}).
SELECT
    f.id,
    f.risk_monitoring_contract_id AS card_id,
    f.xml_source_id,
    f.file_name,
    f.eis_version,
    f.download_status,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.pipeline.status')) AS rm_status,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.pipeline.result')) AS rm_result,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.doc_type.detected_type')) AS detected_type,
    JSON_UNQUOTE(JSON_EXTRACT(f.ai_analysis, '$.resume')) AS resume,
    JSON_EXTRACT(f.ai_analysis, '$.total_doc_risk') AS total_doc_risk,
    JSON_EXTRACT(f.ai_analysis, '$.risks') AS risks,
    JSON_EXTRACT(f.ai_analysis, '$.raw_data.finances') AS finances,
    JSON_EXTRACT(f.ai_analysis, '$.superseded_by') AS superseded_by
FROM risk_monitoring_files f
WHERE f.risk_monitoring_contract_id IN ({ids})

-- Предыдущие версии файлов для сравнения редакций (AIFileAnalyzer). Биндинги: id файлов ({ids}), ещё раз ({ids2}).
-- 1) тот же файл (имя и тип владельца) той же закупки с меньшей версией XML ЕИС;
-- 2) для печатных форм — печатная форма предыдущей версии того же XML-документа (имя файла может отличаться).
-- Ссылка собирается в коде из path / source_url; из нескольких кандидатов берётся максимальная версия.
SELECT
    f.id AS current_id,
    p.id,
    p.eis_version,
    p.file_name,
    p.path,
    p.source_url,
    JSON_EXTRACT(p.ai_analysis, '$.risks[*].rule_id') AS rule_ids,
    JSON_UNQUOTE(JSON_EXTRACT(p.ai_analysis, '$.content_sha256')) AS content_sha256
FROM risk_monitoring_files f
JOIN risk_monitoring_files p
    ON p.risk_monitoring_contract_id = f.risk_monitoring_contract_id
   AND p.id <> f.id
   AND p.file_name = f.file_name
   AND p.owner_type <=> f.owner_type
   AND COALESCE(p.eis_version, 0) < COALESCE(f.eis_version, 0)
WHERE f.id IN ({ids})
UNION ALL
SELECT
    f.id AS current_id,
    p.id,
    COALESCE(p.eis_version, px.eis_version) AS eis_version,
    p.file_name,
    p.path,
    p.source_url,
    JSON_EXTRACT(p.ai_analysis, '$.risks[*].rule_id') AS rule_ids,
    JSON_UNQUOTE(JSON_EXTRACT(p.ai_analysis, '$.content_sha256')) AS content_sha256
FROM risk_monitoring_files f
JOIN risk_monitoring_eis_xml_sources fx ON fx.id = f.xml_source_id
JOIN risk_monitoring_eis_xml_sources px
    ON px.risk_monitoring_contract_id = fx.risk_monitoring_contract_id
   AND px.id <> fx.id
   AND SUBSTRING_INDEX(px.entry_name, '_', 2) = SUBSTRING_INDEX(fx.entry_name, '_', 2)
   AND COALESCE(px.eis_version, -1) < COALESCE(fx.eis_version, -1)
JOIN risk_monitoring_files p
    ON p.xml_source_id = px.id
   AND p.file_name LIKE 'print%form%'
WHERE f.id IN ({ids2})
  AND f.file_name LIKE 'print%form%'

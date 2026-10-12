-- Предыдущие версии файлов для сравнения редакций (AIFileAnalyzer). Биндинги: id файлов ({ids}), ещё раз ({ids2}).
-- 1) тот же файл (имя и тип владельца) той же закупки с меньшей версией XML ЕИС;
-- 2) для печатных форм ЕИС (print_form*, «Печатная форма …», ссылка printForm) — печатная форма той же закупки
--    и того же вида XML (xml_source_type, тег корня XML-источника) с меньшей версией: имя файла может меняться
--    от версии к версии («… (версия 1).html»).
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
LEFT JOIN risk_monitoring_eis_xml_sources fx ON fx.id = f.xml_source_id
JOIN risk_monitoring_files p
    ON p.risk_monitoring_contract_id = f.risk_monitoring_contract_id
   AND p.id <> f.id
   AND p.file_name <> f.file_name
   AND p.owner_type <=> f.owner_type
   AND p.xml_source_type <=> f.xml_source_type
   AND (p.file_name LIKE 'print%form%' OR p.file_name LIKE 'Печатная форма%' OR p.source_url LIKE '%printForm%')
LEFT JOIN risk_monitoring_eis_xml_sources px ON px.id = p.xml_source_id
WHERE f.id IN ({ids2})
  AND (f.file_name LIKE 'print%form%' OR f.file_name LIKE 'Печатная форма%' OR f.source_url LIKE '%printForm%')
  AND COALESCE(p.eis_version, px.eis_version, -1) < COALESCE(f.eis_version, fx.eis_version, -1)
  AND (fx.id IS NULL OR px.id IS NULL
       OR SUBSTRING_INDEX(px.entry_name, '_', 1) = SUBSTRING_INDEX(fx.entry_name, '_', 1))

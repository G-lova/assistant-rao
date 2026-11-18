-- XML результатов/извещений/отмен контрольных проверок за период — для повторного разбора фактов
-- (facts) без модели (risk_monitoring.reparse_checks). Берём документы, по которым строятся проверки:
-- checkResult (результат и решение по жалобе), unplannedCheck/unplannedCheckCancel (извещение и отмена),
-- eventResult (плановая проверка). Биндинги: storage_path (префикс пути архива), затем начало периода (дата).
SELECT
    x.id,
    x.entry_name,
    x.eis_archive_id,
    x.file_name,
    CONCAT(?, a.storage_path) AS archive_url,
    x.ai_analysis
FROM risk_monitoring_eis_xml_sources x
JOIN risk_monitoring_eis_archives a ON a.id = x.eis_archive_id
WHERE (x.entry_name LIKE 'checkResult%'
       OR x.entry_name LIKE 'unplannedCheck%'
       OR x.entry_name LIKE 'eventResult%')
  AND COALESCE(x.created_at, x.updated_at) >= ?
ORDER BY x.id

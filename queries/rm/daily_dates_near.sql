-- Выгрузки ЕИС рядом с датой (диагностика, когда за дату XML нет): дата, архивов, XML.
-- Биндинги: начало и конец окна (даты).
SELECT a.exact_date, COUNT(DISTINCT a.id) AS archives, COUNT(x.id) AS xml
FROM risk_monitoring_eis_archives a
LEFT JOIN risk_monitoring_eis_xml_sources x ON x.eis_archive_id = a.id
WHERE a.exact_date BETWEEN ? AND ?
GROUP BY a.exact_date
ORDER BY a.exact_date

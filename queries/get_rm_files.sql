SELECT 
    id,
    file_name,
    CONCAT(?, path) AS file_path,
    source_url,
    eis_version
FROM risk_monitoring_files
WHERE source_url IN (?)
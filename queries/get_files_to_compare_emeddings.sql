SELECT
    id,
    JSON_EXTRACT(ai_analysis, '$.embeddings') AS embeddings
FROM risk_monitoring_files
WHERE NOT id = ?
AND JSON_EXTRACT(ai_analysis, '$.embeddings') IS NOT NULL
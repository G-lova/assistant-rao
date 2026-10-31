SELECT
    m.id AS media_id,
    m.model_id AS expert_id,
    m.collection_name,
    m.file_name,
    m.disk
FROM media m
WHERE m.model_type LIKE '%User'
  AND m.collection_name IN ('documentPublication', 'documentMonographs') {{EXPERT_FILTER}};

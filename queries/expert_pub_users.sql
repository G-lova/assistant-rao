SELECT
    u.id AS expert_id,
    u.name,
    u.linkPublication,
    u.linkMonographs,
    u.deleted_at
FROM users u
WHERE u.role = 'expert' {{EXPERT_FILTER}};

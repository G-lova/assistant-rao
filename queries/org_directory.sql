SELECT u.organization AS name, u.inn AS inn, u.kpp AS kpp, 'customer' AS kind
FROM users u
WHERE u.role = 'expertise' AND u.deleted_at IS NULL AND u.inn IS NOT NULL AND u.inn != '' AND u.organization IS NOT NULL AND u.organization != ''
UNION ALL
SELECT u.organization AS name, u.organization_inn AS inn, u.organization_kpp AS kpp, 'expert_work' AS kind
FROM users u
WHERE u.role = 'expert' AND u.organization_inn IS NOT NULL AND u.organization_inn != '' AND u.organization IS NOT NULL AND u.organization != ''
UNION ALL
SELECT u.diplom AS name, u.diplom_inn AS inn, u.diplom_kpp AS kpp, 'expert_diplom' AS kind
FROM users u
WHERE u.role = 'expert' AND u.diplom_inn IS NOT NULL AND u.diplom_inn != '' AND u.diplom IS NOT NULL AND u.diplom != '';

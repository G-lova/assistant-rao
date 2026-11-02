SELECT u.id AS expert_id, u.organization, u.organization_inn, u.organization_kpp, u.diplom, u.diplom_inn, u.diplom_kpp
FROM users u
WHERE u.role = 'expert';

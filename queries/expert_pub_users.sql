/* Эксперты и ссылки анкеты на публикации / монографии (синхронизация публикаций экспертов, search_experts.publications).
   Фильтр по экспертам подставляет код (search_experts.publications.sync). */
SELECT
    u.id AS expert_id,
    u.name,
    u.linkPublication,
    u.linkMonographs,
    u.deleted_at
FROM users u
WHERE u.role = 'expert' {{EXPERT_FILTER}};

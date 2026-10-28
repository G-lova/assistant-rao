-- Похожие закупки для сравнения (профиль цены и конкуренции): все закупки системы со снижением цены по итогам
-- процедуры за период. Группировка по классу ОКПД2 — в Python (risk_monitoring.purchase_profiles).
-- Биндинги: начало периода (дата).
SELECT
    c.id,
    c.fz_type,
    COALESCE(JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract.okpd2_code')),
             JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract.ktpy'))) AS okpd2,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.vidz_code')) AS method_code,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.vidz')) AS method,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.protocol.win_discount_pct')) AS discount,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.protocol.app_count')) AS applications
FROM risk_monitoring_contracts c
WHERE COALESCE(c.date_public, DATE(c.created_at)) >= ?
  AND JSON_EXTRACT(c.dataset, '$.protocol.win_discount_pct') IS NOT NULL

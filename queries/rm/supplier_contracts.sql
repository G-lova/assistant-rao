-- Закупки/контракты поставщика за период (аналитика заказчика / поставщика).
-- Биндинги: начало периода (дата), затем ИНН поставщика ({inns}).
SELECT
    c.id,
    c.number,
    c.fz_type,
    c.date_public,
    c.risk_monitoring_organisation_id AS organisation_id,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.nmck')) AS nmck,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.ispolnenie.contr_zena')) AS contract_price,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.vidz_code')) AS method_code,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.izvejenie.vidz')) AS method,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.protocol.single_app_flag')) AS single_app_flag,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.protocol.app_count')) AS app_count,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.protocol.win_discount_pct')) AS win_discount_pct,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract_details.num_contr')) AS contract_number,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract_details.contr_data')) AS contract_date,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract.mod_reason_code')) AS mod_reason_code,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.procedure.term_code')) AS term_code,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.contract.penalty_fact')) AS penalty_fact,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.owner.zak_inn')) AS customer_inn,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.owner.zak_kpp')) AS customer_kpp,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.owner.zak_name_krat')) AS customer_name,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_inn')) AS supplier_inn,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_kpp')) AS supplier_kpp,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_name_krat')) AS supplier_name,
    JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_stat')) AS supplier_status,
    JSON_EXTRACT(c.ai_analysis, '$.profile.score') AS score,
    JSON_UNQUOTE(JSON_EXTRACT(c.ai_analysis, '$.profile.zone')) AS zone,
    JSON_EXTRACT(c.ai_analysis, '$.risks[*].rule_id') AS risk_codes
FROM risk_monitoring_contracts c
WHERE COALESCE(c.date_public, DATE(c.created_at)) >= ?
  AND JSON_UNQUOTE(JSON_EXTRACT(c.dataset, '$.supplier.post_inn')) IN ({inns})

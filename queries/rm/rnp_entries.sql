-- Реестр недобросовестных поставщиков (risk_monitoring_rnp_entries, загружает внешняя система) по ИНН.
-- Биндинги: ИНН ({inns}).
SELECT r.id, r.fz, r.reg_number, r.status, r.has_inclusion, r.supplier_name, r.inn, r.kpp, r.reason_code,
       r.authority_name, r.published_at, r.first_published_at, r.planned_exclusion_date, r.excluded_at,
       r.exclusion_type, r.print_form_url
FROM risk_monitoring_rnp_entries r
WHERE r.inn IN ({inns})

-- Тома многотомного архива закупки («Документация.part01.rar», «….part02.rar» …): файлы той же карточки с тем же
-- началом имени. Биндинги: id карточки, шаблон имени (LIKE).
SELECT f.id, f.file_name, f.file_type
FROM risk_monitoring_files f
WHERE f.risk_monitoring_contract_id = ? AND f.file_name LIKE ?

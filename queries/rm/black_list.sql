-- Реестр лиц, привлечённых по ст. 19.28 КоАП РФ (обновляется каждую ночь), по ИНН.
-- Биндинги: ИНН ({inns}).
SELECT b.id, b.inn, b.kpp, b.ogrn, b.name, b.type, b.sud, b.deal_number, b.date_commit, b.date_begin
FROM zakupki_black_lists b
WHERE b.inn IN ({inns})

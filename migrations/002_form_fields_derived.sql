-- Этап 3: признак «производный ключ» в справочнике полей форм.
-- derived = TRUE — ключа нет в Excel «Отчеты и закупки поля.xlsx», он достроен правилами
-- knowledge_store/forms.py (комментарии <ключ>_text, итог подраздела, служебные поля).
-- Идемпотентна. Откат: ALTER TABLE pe_form_fields DROP COLUMN derived;
ALTER TABLE pe_form_fields ADD COLUMN IF NOT EXISTS derived BOOLEAN NOT NULL DEFAULT FALSE;

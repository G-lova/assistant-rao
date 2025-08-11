CREATE TABLE report_for_operator (
    procurement_id SERIAL PRIMARY KEY, -- id закупки
    expertise_object TEXT, -- Объект экспертизы
    legal_regulation TEXT, -- Законодательное регулирование
    procurement_method TEXT, -- Способ закупки
    expertise_request TEXT, -- Заявка на проведение экспертизы
    acceptance_act TEXT, -- Акт о приемке товара
    contract_date DATE, -- Дата контракта
    works_acceptance_doc TEXT, -- Документ о приемке и/или акт сдачи-приемки работ (услуг)
    goods_acceptance_doc TEXT, -- Документ о приемке товара (УПД, Счет-фактура и др.)
    impossible_alternative_doc TEXT, -- Документация, подтверждающая невозможность (нецелесообразность) использования иных способов определения поставщика
    penalty_recovery_docs TEXT, -- Документы по взысканию пени и штрафов
    warranty_docs TEXT, -- Документы, подтверждающие гарантийные обязательства
    contract_conditions_docs TEXT, -- Документы, подтверждающие исполнение всех условий контракта
    ip_rights_transfer_docs TEXT, -- Документы, подтверждающие передачу авторских прав на результаты интеллектуальной собственности
    goods_origin_docs TEXT, -- Документы, подтверждающие страну происхождения товара
    additional_materials TEXT, -- Дополнительные материалы
    contract_amendments TEXT, -- Дополнительные соглашения к контракту
    notice TEXT, -- Извещение
    nir_contract TEXT, -- Контракт на выполнение НИР (или НИОКР)
    service_contract TEXT, -- Контракт на выполнение работ (оказание услуг)
    goods_contract TEXT, -- Контракт на поставку товара
    price_justification_docs TEXT, -- Материалы, подтверждающие Обоснование н(м)цк
    price_justification TEXT, -- Обоснование н(м)цк
    procurement_description TEXT, -- Описание объекта закупки
    nir_report TEXT, -- Отчет о выполнении НИР
    procurement_policy TEXT, -- Положение о закупках организации
    bid_evaluation_procedure TEXT, -- Порядок рассмотрения и оценки заявок на конкурс
    contract_subject TEXT, -- Предмет контракта
    contract_draft TEXT, -- Проект контракта
    contract_details TEXT, -- Реквизиты контракта
    compliance_certificates TEXT, -- Сертификаты соответствия
    eis_link TEXT, -- Ссылка на ЕИС
    technical_documentation TEXT, -- Техническая документация, паспорт товара и пр.
    goods_invoice TEXT, -- Товарная накладная
    bid_requirements TEXT, -- Требования к содержанию заявки на конкурс
    work_results_photos TEXT, -- Фото результатов выполнения работ (оказания услуг)
    goods_photos TEXT, -- Фото товара
    contract_execution_expertise TEXT -- Экспертиза результатов исполнения контракта
);

CREATE INDEX idx_procurement_id ON report_for_operator (procurement_id);
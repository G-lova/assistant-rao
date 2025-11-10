-- Таблица для хранения извлечённых "сырых" данных из документов
CREATE TABLE raw_document_data (
    id SERIAL PRIMARY KEY,
    procurement_id TEXT NOT NULL,  -- id закупки
    acceptance_act JSONB, -- Акт о приемке товара
    works_acceptance_doc JSONB, -- Документ о приемке и/или акт сдачи-приемки работ (услуг)
    goods_acceptance_doc JSONB, -- Документ о приемке товара (УПД, Счет-фактура и др.)
    impossible_alternative_doc JSONB, -- Документация, подтверждающая невозможность (нецелесообразность) использования иных способов определения поставщика
    penalty_recovery_docs JSONB, -- Документы по взысканию пени и штрафов
    warranty_docs JSONB, -- Документы, подтверждающие гарантийные обязательства
    contract_conditions_docs JSONB, -- Документы, подтверждающие исполнение всех условий контракта
    ip_rights_transfer_docs JSONB, -- Документы, подтверждающие передачу авторских прав на результаты интеллектуальной собственности
    goods_origin_docs JSONB, -- Документы, подтверждающие страну происхождения товара
    additional_materials JSONB, -- Дополнительные материалы
    technical_specification JSONB, -- Техническое задание
    contract_amendments JSONB, -- Дополнительные соглашения к контракту
    description_purchase_object JSONB, --Описание объекта закупки
    notice JSONB, -- Извещение
    nir_contract JSONB, -- Контракт на выполнение НИР (или НИОКР)
    service_contract JSONB, -- Контракт на выполнение работ (оказание услуг)
    goods_contract JSONB, -- Контракт на поставку товара
    price_justification_docs JSONB, -- Материалы, подтверждающие Обоснование н(м)цк
    price_justification JSONB, -- Обоснование н(м)цк
    nir_report JSONB, -- Отчет о выполнении НИР
    procurement_policy JSONB, -- Положение о закупках организации
    bid_evaluation_procedure JSONB, -- Порядок рассмотрения и оценки заявок на конкурс
    contract_draft JSONB, -- Проект контракта
    compliance_certificates JSONB, -- Сертификаты соответствия
    eis_link JSONB, -- Ссылка на ЕИС
    technical_documentation JSONB, -- Техническая документация, паспорт товара и пр.
    goods_invoice JSONB, -- Товарная накладная
    bid_requirements JSONB, -- Требования к содержанию заявки на конкурс
    work_results_photos JSONB, -- Фото результатов выполнения работ (оказания услуг)
    goods_photos JSONB, -- Фото товара
    contract_execution_expertise JSONB, -- Экспертиза результатов исполнения контракта
    eis_data JSONB, -- Проверка доступности ЕИС и номер закупки
    summary_report JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Таблица для хранения итогового "чистого" заключения модели
CREATE TABLE clean_document_conclusions (
    id SERIAL PRIMARY KEY,
    procurement_id TEXT NOT NULL,  -- id закупки
    acceptance_act TEXT, -- Акт о приемке товара
    works_acceptance_doc TEXT, -- Документ о приемке и/или акт сдачи-приемки работ (услуг)
    goods_acceptance_doc TEXT, -- Документ о приемке товара (УПД, Счет-фактура и др.)
    impossible_alternative_doc TEXT, -- Документация, подтверждающая невозможность (нецелесообразность) использования иных способов определения поставщика
    penalty_recovery_docs TEXT, -- Документы по взысканию пени и штрафов
    warranty_docs TEXT, -- Документы, подтверждающие гарантийные обязательства
    contract_conditions_docs TEXT, -- Документы, подтверждающие исполнение всех условий контракта
    ip_rights_transfer_docs TEXT, -- Документы, подтверждающие передачу авторских прав на результаты интеллектуальной собственности
    goods_origin_docs TEXT, -- Документы, подтверждающие страну происхождения товара
    additional_materials TEXT,  -- Дополнительные материалы
    technical_specification TEXT, -- Техническое задание
    contract_amendments TEXT,  -- Дополнительные соглашения к контракту
    description_purchase_object TEXT, -- Описание объекта закупки
    notice TEXT,  -- Извещение
    nir_contract TEXT,  -- Контракт на выполнение НИР (или НИОКР)
    service_contract TEXT, -- Контракт на выполнение работ (оказание услуг)
    goods_contract TEXT, -- Контракт на поставку товара
    price_justification_docs TEXT, -- Материалы, подтверждающие Обоснование н(м)цк
    price_justification TEXT, -- Обоснование н(м)цк
    nir_report TEXT,  -- Отчет о выполнении НИР
    procurement_policy TEXT, -- Положение о закупках организации
    bid_evaluation_procedure TEXT, -- Порядок рассмотрения и оценки заявок на конкурс
    contract_draft TEXT, -- Проект контракта
    compliance_certificates TEXT, -- Сертификаты соответствия
    eis_link TEXT, -- Ссылка на ЕИС
    technical_documentation TEXT, -- Техническая документация, паспорт товара и пр.
    goods_invoice TEXT, -- Товарная накладная
    bid_requirements TEXT, -- Требования к содержанию заявки на конкурс
    work_results_photos TEXT, -- Фото результатов выполнения работ (оказания услуг)
    goods_photos TEXT, -- Фото товара
    contract_execution_expertise TEXT, -- Экспертиза результатов исполнения контракта
    consistency_check TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индексы для производительности
CREATE INDEX idx_raw_procurement_id ON raw_document_data (procurement_id);
CREATE INDEX idx_clean_procurement_id ON clean_document_conclusions (procurement_id);
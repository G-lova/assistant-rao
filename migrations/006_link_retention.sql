-- 006: срок хранения ссылок риск-мониторинга на общий документ (решение 13.10.2026).
-- Ссылка РМ живёт до закрытия закупки/контракта + 12 месяцев (RM_TEXT_AFTER_CLOSE_DAYS), но не дольше
-- 3 лет от загрузки (RM_TEXT_MAX_RETENTION_DAYS). Закрытие — события XML: полное исполнение (CON-006),
-- расторжение (CON-007), отмена закупки (PUR-008), отмена определения поставщика (PUR-016); возобновление
-- закупки (PUR-009) снимает закрытие. legal_hold — бессрочная защита на время разбора.
-- Текст общего документа очищается, когда у него не осталось живых ссылок ни экспертиз, ни РМ.

ALTER TABLE ai_file_links ADD COLUMN IF NOT EXISTS rm_card_id BIGINT;        -- risk_monitoring_contracts.id (карточка закупки)
ALTER TABLE ai_file_links ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE ai_file_links ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;    -- закупка/контракт закрыты
ALTER TABLE ai_file_links ADD COLUMN IF NOT EXISTS legal_hold BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE ai_file_links SET expires_at = linked_at + interval '1095 days' WHERE expires_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_ai_file_links_card ON ai_file_links (env, rm_card_id);
CREATE INDEX IF NOT EXISTS ix_ai_file_links_document ON ai_file_links (document_id);

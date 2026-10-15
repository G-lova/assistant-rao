-- 005: общий документ по содержимому для экспертиз и риск-мониторинга.
-- Текст, чанки, эмбеддинги и кэш шагов LLM хранятся один раз на SHA-256 файла (ai_documents и связанные
-- таблицы из 003), а pe_documents становится ссылкой экспертизы на такой документ (тип, извлечённые данные,
-- срок хранения ссылки). Тексты из pe_documents переносятся в ai_documents; pe_documents.text_full больше
-- не пишется и остаётся до следующей миграции для отката. pe_chunks больше не используется (экспертиза
-- индексируется в ai_document_chunks с нарезкой pages_v2) и будет удалена отдельной миграцией.
-- Требуется 003 (ai_*). Все операторы идемпотентны.

-- Документ: каким чтением получен текст, число страниц, когда текст очищен политикой хранения
ALTER TABLE ai_documents ADD COLUMN IF NOT EXISTS reader TEXT;            -- file_reader | rm_pdf_text
ALTER TABLE ai_documents ADD COLUMN IF NOT EXISTS pages_count INTEGER;
ALTER TABLE ai_documents ADD COLUMN IF NOT EXISTS text_purged_at TIMESTAMPTZ;

-- Чанки: схема нарезки, страницы и заголовок раздела
ALTER TABLE ai_document_chunks ADD COLUMN IF NOT EXISTS chunker TEXT NOT NULL DEFAULT 'rm_tokens_800';
ALTER TABLE ai_document_chunks ADD COLUMN IF NOT EXISTS page_from INTEGER;
ALTER TABLE ai_document_chunks ADD COLUMN IF NOT EXISTS page_to INTEGER;
ALTER TABLE ai_document_chunks ADD COLUMN IF NOT EXISTS heading TEXT;
ALTER TABLE ai_document_chunks DROP CONSTRAINT IF EXISTS ai_document_chunks_document_id_chunk_no_embedding_model_key;
ALTER TABLE ai_document_chunks DROP CONSTRAINT IF EXISTS ai_document_chunks_doc_chunker_model_no_key;
ALTER TABLE ai_document_chunks ADD CONSTRAINT ai_document_chunks_doc_chunker_model_no_key
    UNIQUE (document_id, chunker, embedding_model, chunk_no);
-- Частичные HNSW-индексы по схеме нарезки: поиск похожих документов РМ не смешивается с чанками экспертизы.
-- Запросы фильтруют chunker литералом — только так планировщик выбирает частичный индекс.
CREATE INDEX IF NOT EXISTS idx_ai_chunks_emb_rm ON ai_document_chunks USING hnsw (embedding vector_cosine_ops) WHERE chunker = 'rm_tokens_800';
CREATE INDEX IF NOT EXISTS idx_ai_chunks_emb_pages ON ai_document_chunks USING hnsw (embedding vector_cosine_ops) WHERE chunker = 'pages_v2';
DROP INDEX IF EXISTS idx_ai_chunks_embedding;

-- Ссылка экспертизы на общий документ
ALTER TABLE pe_documents ADD COLUMN IF NOT EXISTS ai_document_id BIGINT REFERENCES ai_documents(id);
CREATE INDEX IF NOT EXISTS ix_pe_documents_ai_document ON pe_documents (ai_document_id);

-- Перенос текстов: один документ на хэш файла. Если документ уже есть (его прочитал РМ), его текст остаётся,
-- текст экспертизы берётся только когда у документа текста нет.
INSERT INTO ai_documents (content_sha256, text_sha256, file_ext, text, ocr_status, reader, pages_count, text_purged_at)
SELECT DISTINCT ON (p.sha256)
       p.sha256,
       CASE WHEN p.text_full IS NOT NULL THEN encode(sha256(convert_to(p.text_full, 'UTF8')), 'hex') END,
       lower(substring(p.filename FROM '\.[^.]+$')),
       p.text_full,
       'done',
       'file_reader',
       p.pages_count,
       CASE WHEN p.text_full IS NULL THEN COALESCE(p.text_purged_at, NOW()) END
FROM pe_documents p
WHERE p.sha256 ~ '^[0-9a-f]{64}$'
ORDER BY p.sha256, (p.text_full IS NULL), p.created_at DESC
ON CONFLICT (content_sha256) DO UPDATE SET
    text = EXCLUDED.text, text_sha256 = EXCLUDED.text_sha256, ocr_status = 'done', reader = EXCLUDED.reader,
    pages_count = EXCLUDED.pages_count, text_purged_at = NULL, updated_at = NOW()
WHERE ai_documents.text IS NULL AND EXCLUDED.text IS NOT NULL;

UPDATE pe_documents p SET ai_document_id = a.id
FROM ai_documents a
WHERE a.content_sha256 = p.sha256 AND p.ai_document_id IS NULL;

# Риск-мониторинг: анализ файлов и XML-событий

Эндпоинты, формат `ai_analysis` для внешней системы и покрытие концепции «Система риск-мониторинга».

## Эндпоинты

Новые эндпоинты; существующие (`/get-ai-analysis/file` и др.) не менялись. Авторизация — как у остальных (`APIKeyMiddleware`), окружение — заголовок `X-API-Database` (`dev` / `stage` / `prod`).

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| POST | `/risk-monitoring/files/analysis` | `{"ids": [1097, 1096], "push": true, "force": false, "recompute": false, "include_analysis": false}` | `202 {"task_id", "count", "status": "queued"}` |
| POST | `/risk-monitoring/xml/analysis` | `{"ids": [812345], "push": true, "force": false, "include_analysis": false}` | `202 {"task_id", "count", "status": "queued"}` |
| GET | `/task/{task_id}` (существующий) | — | `summary` (счётчики `done` / `skipped` / `error` / `not_found` / `pushed`) и `results` по каждому id |

- `ids` — до 500 id; повторы убираются.
- **Повторного анализа нет.** Запись с `ai_analysis.pipeline.status = completed` пропускается (`skipped: already_analyzed`). Неудачная (`failed`) берётся повторно до 3 попыток.
- `force` (анализировать заново) и `recompute` (игнорировать кэш LLM) — только при `RISK_MONITORING_DEBUG=true`; иначе 403.
- `push: false` — пробный прогон: результат не пишется во внешнюю БД и возвращается в задаче вместе с `include_analysis: true`.
- Задачи идут в очередь `risk_monitoring` и обрабатываются отдельным воркером `celery_worker_rm` (`docker-compose.yml`).

Порядок для ночного прогона: сначала файлы, затем XML. Тогда событие XML получает резюме уже проанализированных файлов.

## Формат `risk_monitoring_files.ai_analysis` (паспорт документа)

`schema_version = "rm-file-1"`. Поля, которые читает паспорт документа во внешней системе:

| Поле | Содержимое |
|---|---|
| `doc_type` | `detected_type`, `type_decode`, `confidence` (0–1), `issues`, `key_indicators` |
| `readability` | `status`, `readability_score`, `main_language`, `language_confidence`, `issues`, `pages` (по разметке страниц; `null` для форматов без страниц), `text_chars`, `ocr` |
| `analyzed_at`, `model` | Время анализа, модель |
| `resume`, `confidence` | AI-заключение (5–10 предложений) и уверенность (0–1) |
| `raw_data` | Извлечённые данные с `evidence` (`fragment`, `section`, `page`, `confidence`): `document_name`, `summary`, `contract_number`, `dates`, `finances`, `organizations`, `law_references`, `procurement_subject`, `items`, `planned_timeline`, `execution_location`, `other_data`, … |
| `total_doc_risk` | Интегральный риск 0–1 = `1 − Π(1 − level × confidence)` по рискам документа |
| `risks[]` | `rule_id`, `title`, `description`, `level` (0–1), `confidence` (0–1), `law`, `evidence[]` (`fragment`, `section`, `page`, `confidence`, `quote_verified`), `verification_needed[]`, а также `category`, `severity`, `sources[]` (`ai_file` / `version_diff`). Один код — один риск: фрагменты одного кода объединены, потому что во внешней БД уникальность по `(file_id, rule_id)` |
| `similar_doc_ids[]` | `id` (id файла), `similarity`, `coverage`, `near_duplicate`, `exact_text_duplicate` |

Расширение по концепции (интерфейс пока не показывает, в `unknown_keys`):

| Поле | Содержимое |
|---|---|
| `passport` | `general` (имя, тип, размер, владелец, версия XML, закупка), `purpose`, `structure` (`main_sections`, `has_tables`, `has_appendices`, `appendices`), `key_conditions[]`, `semantics` (`topics`, `keywords`, `classification`), `technical_objects` (`brands`, `models`, `manufacturers`, `standards`), `assessment` (`completeness`, `structuredness`, `document_quality`, `legal_elaboration`, `competition_restriction` — 0–1), `recommendations[]`, `confidence` |
| `risk_profile` | По категориям (`DOC`, `FIN`, `PROC`, `AI` …): `count`, `max_level`, `score` |
| `risks_comparison` | Сравнение с предыдущей версией документа: `persisting`, `new`, `not_found_in_new_version`. Риск из `not_found_in_new_version` считается **снятым**; `null`, если предыдущей версии нет |
| `version_changes` | `previous_file_id`, `previous_eis_version`, `identical`, `similarity`, `stats` (абзацев добавлено / удалено / заменено), `summary`, `risk_relevant`, `changes[]` (`type`, `subject`, `section`, `before`, `after`, `impact`, `risk_effect`, `significance`) |
| `content_sha256` | Хэш содержимого файла |
| `pipeline` | `schema_version`, `catalog_version`, `stage` (`S1b`), `status` (`completed` / `failed`), `attempt`, `debug`, `previous_scope_id`, `model`, `prompt_versions`, `analyzed_at`, `result` (`no_text`), `error` |

Неудачная попытка пишет только `{"pipeline": {"status": "failed", ...}}`. Документ без текста — законченный результат: `pipeline.result = "no_text"`, `readability.status = "deny"`.

Эмбеддинги во внешнюю БД **не отправляются**: они хранятся в Postgres сервиса (`ai_document_chunks`).

## Формат `risk_monitoring_eis_xml_sources.ai_analysis` (событие)

`schema_version = "rm-xml-1"`.

| Поле | Содержимое |
|---|---|
| `event` | `code` (`PUR-*`, `CON-*`, `CMP-001`, `CTL-001`, `RNP-001`, `RPT-001`, `PLN-001`, `OTH-000`), `title`, `group`, `importance` (`low` / `medium` / `high` / `critical`), `subtypes[]` (`code`, `title`), `first_version`, `previous_missing`, `xml_tag`, `eis_version`, `event_at`, `previous_xml_id`, `previous_eis_version`, `significance` |
| `facts` | Номер закупки, реестровый номер контракта, ИКЗ и его части (`ikz_parts`: год, ИНН/КПП заказчика, ОКПД2, КВР), НМЦК, цена, способ, предмет, даты, заказчик, поставщик, основание изменения (`modification_reason_code`), расторжение |
| `changes[]` | `path`, `label`, `old`, `new`, `change` (`added` / `removed` / `modified`), `delta`, `delta_pct` (до 200 шт.; всего — `changes_total`) |
| `changes_assessment[]` | Оценка значимых изменений моделью: `change_type`, `description`, `old_value`, `new_value`, `severity`, `change_justification` |
| `documents` | `added[]`, `removed[]` (`file_id`, `file_name`, `description`, `url`), `replaced[]` (`before_file_id`, `after_file_id`, `file_name`, `before_url`, `after_url`) |
| `files[]` | Файлы события: `file_id`, `file_name`, `detected_type`, `total_doc_risk` |
| `resume`, `confidence` | Суть события (LLM или шаблон без LLM) |
| `risks[]`, `total_event_risk` | Тот же формат рисков, что у файлов; `sources`: `rule` (правила кода, уверенность 1.0) и `ai_event` (LLM) |
| `link` | Только для XML без `risk_monitoring_contract_id`: `method` (`purchase_number` / `contract_number` / `ikz` / `ikz_customer` / `none`), `risk_monitoring_contract_id`, `organisation_id`, `ambiguous`, `candidates[]`, `customer_inn`, `customer_kpp`. **PHP применяет привязку**, если `method ≠ none` и `ambiguous = false` |
| `pipeline` | Как у файлов, `stage = "S1a"`, `export_date` — дата выгрузки архива |

## Каталог рисков и событий

`risk_monitoring/risk_catalog.py` — единственный источник кодов. Промпты и JSON-схемы получают список кодов оттуда.

⚠️ Коды `DOC-001…DOC-010` совпадают с новым промптом `risk_analysis_prompt.txt` (DOC-001 — товарный знак). В старом `doc_risks_detector.py` те же коды значили другое (DOC-001 — «Неполнота документа»). Строки `risk_monitoring_document_risks`, созданные старым детектором, нужно пересоздать.

Коды событий по концепции (гл. 11.4–11.5), но контрактные события имеют префикс `CON-`, чтобы не совпадать с кодами рисков `CTR-*`.

## Покрытие концепции

| Концепция | Файлы | XML-события |
|---|---|---|
| 18.2 Конвейер: тип, OCR, структура, сущности, классификация, LLM, AI-паспорт | ✓ | — |
| 6.4.6.10 / 18.4 AI-паспорт: общая информация, структура, семантика, качество, итоговая оценка | ✓ (`passport`, `readability`, `doc_type`) | — |
| 18.5 Сущности: организации/ИНН, суммы, сроки, нормы, бренды/модели/производители/ГОСТ | ✓ (`raw_data`, `passport.technical_objects`) | ✓ (`facts`) |
| 18.6 Семантический анализ: цель, требования, условия, противоречия, риски | ✓ | — |
| 18.7–18.8 Сравнение редакций и смысловой diff | ✓ (`version_changes`, риск `DOC-011`) | ✓ структурный diff (`changes`) |
| 18.10 Риск-факторы: финансовые, процедурные, технические, контрактные | ✓ (FIN-003…006, FIN-009, PROC-003/008, DOC-001…010, AI-*) | ✓ (FIN-007, CTR-*, PROC-*) |
| 18.11 / «Доказательная база AI»: документ → страница → раздел → фрагмент, проверка цитаты | ✓ (абзац — нет) | ✓ (путь в XML, было/стало) |
| 18.12 AI-заключение: описание, особенности, риски, уверенность, рекомендации | ✓ (`resume`, `passport.recommendations`) | ✓ (`resume`) |
| 18.13 База знаний: похожие документы, повторное использование шаблонов | ✓ (`similar_doc_ids`, `near_duplicate`) | — |
| 7.7 AI-объяснение риска | ✓ (`description`, `evidence`, `law`, `verification_needed`) | ✓ |
| 11.3–11.6 Типы событий закупки, контракта, документов | — | ✓ (`event`, `documents`) |
| 11.7 События риска: новый / снят | ✓ (`risks_comparison`) | — (уровень закупки) |
| 11.8 Структура события: тип, объект, время, было/стало, изменённые поля, важность | — | ✓ |
| 15.5–15.6 Изменения условий контракта: основание, стоимость/срок до и после, AI-анализ | — | ✓ |
| Привязка XML без закупки (номер → ИКЗ → заказчик) | — | ✓ (`link`) |
| 18.9 Семантический граф, 18.14 библиотека риск-паттернов | — | — (вне MVP) |
| 11.12 Цепочки событий, 14.7 индекс изменчивости, 10.x скоринг закупки | — | — (уровень закупки, этап S3) |

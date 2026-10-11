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
| `risks[]` | `rule_id`, `title`, `description`, `level` (0–1), `confidence` (0–1), `law` (по закону закупки, см. «Каталог»), `evidence[]` (`fragment`, `section`, `page`, `confidence`, `quote_verified`), `verification_needed[]`, а также `category`, `kind`, `indicator`, `fz`, `law_model`, `severity`, `sources[]` (`ai_file` / `version_diff`). Один код — один риск: фрагменты одного кода объединены, потому что во внешней БД уникальность по `(file_id, rule_id)` |
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

Каталог рисков — `rm-catalog-2`, 93 кода, полная таблица в `docs/risk_catalog.md`:
- каждому из 41 показателя внешней системы (`pz_p1 … pc_n6`) соответствует ровно один код, сами показатели не меняются;
- риски концепции без показателя — отдельные коды;
- у каждого риска две нормы: по 44-ФЗ и по 223-ФЗ.

Документ — источник истины. Из него скрипт `python -m scripts.build_risk_catalog` собирает `risk_monitoring/config/risk_catalog.json`, который читают `risk_monitoring/risk_catalog.py` и, при необходимости, PHP. Тест `test_catalog_json_matches_document` следит, чтобы JSON не отставал от документа. После правки таблицы:

```bash
python -m scripts.build_risk_catalog          # пересобрать JSON
python -m scripts.build_risk_catalog --check  # проверить, что JSON актуален
```

Промпты и JSON-схемы получают списки разрешённых кодов из `risk_catalog.py`:
- `DOCUMENT_RISK_CODES` — анализ файлов;
- `EVENT_RISK_CODES` — XML-события;
- `VERSION_RISK_CODES` — сравнение редакций.

**Поля риска, которые задаёт каталог** (во всех `risks[]`):
- `law` — норма по закону закупки: `fz_type` карточки для файлов, `fz` архива или карточки для XML. Например, «Федеральный закон № 223-ФЗ: ч. 25 ст. 3.2; …». Если закон не определён, указываются нормы обоих законов через « | ». Норма модели, если она отличается от каталожной, сохраняется в `law_model`;
- `kind` — `risk` / `violation` / `ai`;
- `indicator` — показатель внешней системы (`pz_p1` …) или `null`;
- `fz` — 44 / 223 / `null`.

Риски, неприменимые к закону закупки, отбрасываются. Например, PROC-007 (ст. 111 44-ФЗ) для 223-ФЗ.

**Детерминированные риски XML-событий:**

| Событие | Код |
|---|---|
| Изменение НМЦК ≥ 10 % | FIN-013 |
| Изменение цены контракта | CTR-004 |
| Изменение сроков | CTR-005 |
| Доп. соглашение по коду основания | 011 → CTR-004, 012 → CTR-008, 021 → CTR-005, 051 → CTR-007, иное → CTR-014 |
| Претензии | CTR-019 |
| Расторжение или односторонний отказ | CTR-020 |
| Уклонение | PROC-009 |
| Нет заявок | PROC-001 |
| Отзыв заявки | PROC-005 |
| Отмена | PROC-002 |
| Жалобы | PROC-008 |

**Сравнение редакций документа:**
- DOC-011 — содержательное изменение, низкий уровень;
- DOC-012 — изменение в сторону риска;
- AI-004 — текст почти не изменился (схожесть ≥ 0,9), а смысл изменился существенно.

⚠️ Старый `risk_monitoring/doc_risks_detector.py` (через `FileProcessor` его использует существующий `/get-ai-analysis/file`) работает со старыми кодами концепции, и их значения не совпадают с `rm-catalog-2`. Результаты этого эндпоинта нельзя раскладывать в `risk_monitoring_document_risks` вместе с новыми.

Коды событий — по концепции (гл. 11.4–11.5), но контрактные события имеют префикс `CON-`, чтобы не совпадать с кодами рисков `CTR-*`.

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

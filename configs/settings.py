"""Переключатели и лимиты сервиса — в коде, а не в ``.env``. Единственный файл настроек сервиса: и экспертизы
(«РАО Эксперт»), и риск-мониторинг (раздел «Риск-мониторинг» ниже).

В ``.env`` остаются только адреса, ключи, пароли, названия моделей и ``APP_ENV`` (они разные на dev / stage / prod).
Всё, что меняет поведение сервиса, задаётся здесь и попадает в историю репозитория вместе с кодом. Окружение
установки по умолчанию (``ENVIRONMENT``) выводится из ``APP_ENV``: от него зависят ``PE_LEGACY_ENV``,
``DAILY_ENVIRONMENT`` и ``TYPAL_REGIONS_ENVIRONMENT``.

Значения читаются при каждом вызове (``settings.X``), поэтому в тестах их можно подменять
``mock.patch.object(settings, "X", ...)``.
"""

import os

# Окружение установки, если APP_ENV не задан ни в .env, ни в окружении процесса. Единственная строка, которая
# различается между ветками: в master (прод) — "prod", в остальных — "stage". При переносе кода в master строку
# не перезаписывать; CI прода проверяет, что ENVIRONMENT == "prod" (.gitlab-ci.yml)
DEFAULT_ENV = "prod"

# Окружение установки из APP_ENV (.env; не задан — DEFAULT_ENV): development → dev, staging → stage, production → prod,
# neuro_assistant_database → neuro; так же его понимает Config.get_database_config
ENV_ALIASES = {"development": "dev", "develop": "dev", "staging": "stage", "production": "prod",
               "neuro_assistant_database": "neuro"}


def normalize_env(value: str) -> str:
    """Имя окружения в ключах хранилища (``dev`` / ``stage`` / ``prod`` / ``neuro``) из значения ``APP_ENV``.

    Args:
        value: Значение ``APP_ENV`` (``development``, ``stage``, ``Production`` …).

    Returns:
        str: Короткое имя окружения в нижнем регистре.
    """
    v = (value or "").strip().lower()
    return ENV_ALIASES.get(v, v or DEFAULT_ENV)


try:  # APP_ENV может быть только в файле .env (вне docker), а settings импортируется раньше Config
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass
ENVIRONMENT = normalize_env(os.getenv("APP_ENV", DEFAULT_ENV))

# Перезапуск API при изменении файлов (uvicorn --reload; запуск — scripts/run_api.py). На проде выключен: код
# приходит деплоем, контейнер пересоздаётся после сборки и тестов (.gitlab-ci.yml), а с --reload API переходил на
# новый код сразу после git pull — до тестов, которые должны были оставить прод на прежней версии; кроме того,
# наблюдатель файлов держит ресурсы на весь примонтированный каталог. На dev / stage — как раньше
API_RELOAD = ENVIRONMENT != "prod"


# Хранилище знаний («РАО Эксперт»): запись результатов /evaluate-documents в pe_* таблицы
KNOWLEDGE_STORE_ENABLED = True
# Построение фактов сразу после индексации (задача index_documents_task); нужно хранилище знаний
PE_FACTS_ENABLED = True
# Срок хранения полных текстов, чанков и эмбеддингов экспертиз (дней) — политика утверждена 05.10.2026
PE_TEXT_RETENTION_DAYS = 365
# Общий документ по хэшу файла: /evaluate-documents берёт готовый текст из ai_documents вместо повторного чтения/OCR
DOC_DEDUP_ENABLED = True
# Кэш результата TypeDataExtractor: тот же файл с тем же типом, объектом и именем повторно модели не отправляется
# (параметр force в /evaluate-documents кэш обходит)
DOC_EXTRACTION_CACHE_ENABLED = True
# Лимит ответа модели при извлечении данных из документа (data_extractor, type_data_extractor; экспертиза и РМ).
# 3000 не хватало: ответы обрывались на ~10 тыс. символов («Unterminated string», часть данных терялась, 30.10.2026).
# При длинном запросе ответ всё равно уменьшается до размера контекста (configs.llm_resilience.fit_output)
EXTRACTION_MAX_TOKENS = 4000
# Схема ответа извлечения данных через guided_json (evaluate_documents/data_extractor.py). 15.11.2026 сервер
# prod_multimodal_llm на большой схеме извлечения (42 КБ) отвечал «{ Register Register …» — с маленькими схемами
# (тип документа) всё в порядке. False — схема только в промпте (пример JSON в data_extractor_prompt.txt)
EXTRACTION_GUIDED_JSON = True
# Ответ со схемой не дал JSON-объекта (мусор, зацикливание) — блок повторяется один раз без guided_json
EXTRACTION_GUIDED_FALLBACK = True
# Сопоставление экспертизы с карточкой закупки риск-мониторинга после индексации (запросы к внешней БД)
PE_RM_LINK_ENABLED = True
# Параллельность /evaluate-documents на один вызов: запросы к LLM, файлы (чтение/OCR + извлечение), скачивание ссылок
EVAL_LLM_CONCURRENCY = 3
EVAL_FILE_CONCURRENCY = 3
EVAL_LINK_CONCURRENCY = 2
# Чтение PDF в /evaluate-documents (configs.file_reader.FileReader.read_pdf_file): страницы с пригодным текстовым
# слоем не отправляются в OCR. False — как раньше, OCR каждой страницы
EVAL_PDF_TEXT_LAYER = True
# Страницы с растровыми изображениями (скан, подпись, печать) всё равно распознаются моделью: она описывает подписи
# и печати в visual_notes. False — брать текстовый слой и у таких страниц (быстрее, но без описания подписей/печатей)
EVAL_PDF_OCR_IMAGE_PAGES = True
# Запросы к порталу экспертиз с ключом SYSTEM_API_KEY (/api/media, /api/database/query): у портала ограничение
# частоты на ключ, общее для всех запросов. После 429 пауза ставится на весь хост: по Retry-After, а без него —
# PORTAL_RATE_LIMIT_DELAY с, растёт ×2 с каждой попыткой, не больше PORTAL_RATE_LIMIT_MAX_DELAY
PORTAL_RATE_LIMIT_DELAY = 10.0
PORTAL_RATE_LIMIT_MAX_DELAY = 60.0
# Скачивание файлов через /api/media: одновременных запросов к одному хосту в процессе и число повторов после 429
MEDIA_DOWNLOAD_CONCURRENCY = 1
MEDIA_RATE_LIMIT_RETRIES = 6
# SQL-запросы через /api/database/query (DataFetcher): одновременных запросов к хосту в процессе и повторов после 429
PORTAL_QUERY_CONCURRENCY = 4
PORTAL_QUERY_RATE_LIMIT_RETRIES = 4
# Окружение строк, записанных до появления env в ключах (миграции 004, 008), и вызовов без X-API-Database —
# окружение установки (APP_ENV)
PE_LEGACY_ENV = ENVIRONMENT

# Нарезка экспертизы для индексации и поиска фактов (knowledge_store.pages.current_chunker): pages_1500 (прежняя)
# или pages_v2 (по структуре; переключать после диагностики scripts.eval_chunkers variants)
PE_CHUNKER = "pages_1500"
# Что делать с «0», найденным моделью по тексту документов (факты, презумпция, единый стиль): "suspect" — в значение
# не ставить (замечание в trace.suspected, для критерия соответствия ставится «1» как предложение); "value" — ставить
# «0» как предложение после повторной проверки (judge). На эталоне экспертов точность таких «0» 10–30 %
PE_MODEL_ZEROS = "suspect"
# Максимум времени на построение фактов (запросы к LLM), секунд; при превышении этап прерывается
PE_FACTS_TIMEOUT_SEC = 900
# Поиск фрагментов для фактов: сколько брать в основной запрос и в повторную проверку
PE_FACTS_TOP_K = 8
PE_FACTS_RECHECK_K = 16

# Пул соединений с Postgres сервиса в процессе API (configs/working_with_db.open_app_pool, lifespan FastAPI): раньше
# пул создавался и закрывался на каждую операцию
DB_APP_POOL_MAX_SIZE = 10

# Параметры генерации основной модели (MODEL_NAME)
MODEL_TEMPERATURE = 0.7
MODEL_MAX_TOKENS = 4096
# Размер пакета запроса эмбеддингов
EMBEDDING_BATCH_SIZE = 16
# Одновременных запросов к одному сервису эмбеддингов на процесс — общий лимит для всех клиентов (configs/embedding_client.py).
# Раньше у каждого экземпляра EmbeddingClient был свой лимит 10, а экземпляров около десятка
EMBEDDING_CONCURRENCY = 8

# ---------------------------------------------------------------- рейтинг экспертов (POST /get-experts-rating)
# Без флага by_period рейтинг считается с этой даты по завтрашнюю дату (текущий день целиком; дата — по часовому поясу
# EXPERT_RATING_TIMEZONE); с флагом — за период запроса
# С этой же даты считаются показатели и критерии экспертов при подборе (experts_for_expertise.sql, {{RATING_START}};
# раньше — за последний год)
EXPERT_RATING_DEFAULT_START = "2025-01-01"
EXPERT_RATING_TIMEZONE = "Europe/Moscow"

# ---------------------------------------------------------------- конфликт интересов (подбор экспертов)
# Место работы и организация, выдавшая диплом, сравниваются с заказчиком по ИНН/КПП (users.organization_inn / _kpp,
# diplom_inn / _kpp); у кого реквизитов нет — по названиям, как раньше. Тот же ИНН с другим КПП — головная организация
# и её филиал (одно юрлицо): False — конфликт, True — конфликт только при совпадении и ИНН, и КПП
CONFLICT_INN_SAME_KPP_ONLY = False
# Без реквизитов (старый метод): fuzz.ratio ≥ 85 названий организаций по целой строке. В users от 02.11.2026 названия —
# полные официальные, общая ОПФ («ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ …») даёт ratio ≥ 85 почти любой паре вузов:
# 25 991 совпадение «эксперт ↔ заказчик», ни одно не подтверждено ИНН. False — только same_organization (без ОПФ)
CONFLICT_ORG_NAME_FUZZY = False
# Эксперт без ИНН: ИНН восстанавливается по названию из справочника организаций (search_experts.org_directory:
# официальные названия портала + псевдонимы org_name_aliases, TF-IDF символьных n-грамм по названию без ОПФ).
# Кроме сходства ≥ THRESHOLD названия должны совпасть по словам в обе стороны (search_experts.org_directory.covers:
# «Новосибирский государственный университет» ≠ «… технический университет»); ближайшая другая организация не ближе
# MARGIN. Названия подразделений («… институт (филиал) ФГБОУ ВО X») — с порогом BRANCH_THRESHOLD. Справочник
# обновляется раз в TTL_SEC на процесс. Проверка 02.11.2026 (404 эксперта с ИНН, без своей записи в справочнике):
# ИНН восстановлен у 285, верно 281 (99 %; расхождения — в основном эксперт сменил место работы); пары «эксперт
# работает у заказчика» — 237 из 265, 2 ложных. Без ИНН: место работы 327 → 94 с ИНН, диплом 479 → 69
CONFLICT_ORG_DIRECTORY_ENABLED = True
CONFLICT_ORG_DIRECTORY_THRESHOLD = 0.8
CONFLICT_ORG_DIRECTORY_BRANCH_THRESHOLD = 0.7
CONFLICT_ORG_DIRECTORY_MARGIN = 0.03
CONFLICT_ORG_DIRECTORY_TTL_SEC = 3600
# Соавторство (search_experts.coauthors, публикации — миграция 011): соавтор — контактное лицо или руководитель
# заказчика → эксперт исключается; кандидаты-соавторы → остаётся более подходящий. Учитываются публикации последних
# CONFLICT_COAUTHOR_YEARS лет (год неизвестен — учитывается; None — все)
CONFLICT_COAUTHORS_ENABLED = True
CONFLICT_COAUTHOR_YEARS = 5
# Пополнение псевдонимов (org_aliases_task), время московское — до синхронизации публикаций (01:20)
ORG_ALIASES_HOUR = 1
ORG_ALIASES_MINUTE = 5

# ---------------------------------------------------------------- публикации экспертов (подбор экспертов)
# Файлы (media: documentPublication / documentMonographs) и ссылки анкеты (linkPublication / linkMonographs)
# разбираются во внутреннюю БД (миграция 011) и учитываются в similarity при подборе экспертов
EXPERT_PUB_SYNC_ENABLED = True
# Синхронизация с внешней БД: снимок источников всех экспертов (≈ 1,5 тыс. файлов и 3 тыс. ссылок — два SQL-запроса)
# сравнивается с внутренним; новые — в очередь, исчезнувшие — removed. Раз в сутки (Celery beat, время московское):
# до ночной очистки текстов (03:30) и утреннего прогона риск-мониторинга (05:40–06:00). Чтобы не ждать до ночи,
# портал вызывает POST /expert-publications/sync с expert_ids после сохранения анкеты
EXPERT_PUB_SYNC_HOUR = 1
EXPERT_PUB_SYNC_MINUTE = 20
# Источников, обрабатываемых за один запуск задачи (скачивание, чтение/OCR, модель); остальные — в следующий
EXPERT_PUB_BATCH = 40
# Время на обработку за запуск, с (меньше task_soft_time_limit = 1500)
EXPERT_PUB_BUDGET_SEC = 1200
# Неудавшийся источник повторяется не чаще раза в столько часов и не больше EXPERT_PUB_MAX_ATTEMPTS раз
EXPERT_PUB_RETRY_HOURS = 12
EXPERT_PUB_MAX_ATTEMPTS = 3
# Какие ссылки анкеты обрабатывать. eLibrary (63 % ссылок) не обрабатывается: правила сайта запрещают
# автоматический сбор, при массовых запросах — капча и блокировка IP. Scopus — только с ключом Elsevier API;
# ResearchGate / Google Scholar блокируют сбор. Такие ссылки хранятся со статусом unsupported
EXPERT_PUB_LINK_TYPES = ("doi", "file", "cloud", "journal")
# Метаданные по DOI (title, abstract, авторы, год, журнал): OpenAlex, при отказе — Crossref
EXPERT_PUB_OPENALEX_URL = "https://api.openalex.org/works/doi:"
EXPERT_PUB_CROSSREF_URL = "https://api.crossref.org/works/"
EXPERT_PUB_HTTP_TIMEOUT = 30
# Текст файла, передаваемый модели для разбора (символов на запрос) и максимум запросов на файл
# (список трудов на 300 позиций режется на части)
EXPERT_PUB_LLM_CHUNK_CHARS = 12000
EXPERT_PUB_LLM_MAX_CHUNKS = 6
EXPERT_PUB_MAX_PER_SOURCE = 300
# Similarity по публикациям: среднее сходство K самых близких публикаций эксперта с текстом экспертизы, вклад
# публикации убывает вдвое за HALF_LIFE лет, но не ниже FLOOR (старая монография по профилю всё же значима)
EXPERT_PUB_SIMILARITY_ENABLED = True
EXPERT_PUB_TOP_K = 3
EXPERT_PUB_HALF_LIFE_YEARS = 7
EXPERT_PUB_RECENCY_FLOOR = 0.5
# Итоговая similarity эксперта с публикациями: (1 − w) × сходство анкеты + w × сходство публикаций. Эксперт без
# разобранных публикаций сохраняет сходство анкеты (не штрафуется)
EXPERT_PUB_SIMILARITY_WEIGHT = 0.4
# Публикации, где фамилии эксперта нет среди авторов (author_verified = false), не учитываются
EXPERT_PUB_REQUIRE_AUTHOR = True

# Повторы HTTP-запросов (async_retry): попыток, первая задержка (с), множитель задержки
RETRY_MAX_ATTEMPTS = 3
RETRY_DELAY = 1.0
RETRY_BACKOFF = 2.0

# ================================================================ Риск-мониторинг

# Фильтр типовых формулировок (boilerplate.py): снимать риск, если цитата встречается у стольких разных
# заказчиков (закупки РМ и экспертизы)
BOILERPLATE_ENABLED = True
BOILERPLATE_MIN_SOURCES = 3

# Риски модели в паспорте файла по кодам, которые внешняя система считает правилом (FIN-004…008, CTR-002 …), не
# ставятся: решает правило по данным ЕИС (в закупке — drop_rule_covered)
FILE_DROP_RULE_COVERED = True   # снимать только при 44-ФЗ и посчитанном нулевом значении правила (rule_decision)

# Проверка норм, на которые ссылается модель (поле law), по загруженным редакциям актов (legal_refs, миграция 014):
# к риску добавляется law_check (found / lost_force / not_found); балл не меняется. Без загруженных актов — пропуск
LEGAL_REFS_CHECK_ENABLED = True
# Тексты норм в запросе риск-анализа (law_context): ближайшие к документу нормы редакции на дату закупки
# (date_public карточки) из загруженных актов + закреплённые по типу документа; по 223-ФЗ — положение о закупке
# заказчика на ту же дату (способы закупки, ближайшие пункты). Меняет версию кэша риск-анализа только у документов,
# которые анализируются (новые версии), уже готовые анализы не пересчитываются
LEGAL_RAG_ENABLED = True
CLAUSE_RAG_ENABLED = True

# Анализировать ли файл прошлой редакции, если уже известна новая (superseded_by); в режиме отладки (force) —
# анализируется всегда
ANALYZE_SUPERSEDED = False

# Попыток на запрос к модели для чтения, определения типа и извлечения данных (перезапуск модели длится минуты)
LLM_PROXY_RETRIES = 6

# Режим отладки: повторный анализ уже проанализированных файлов и XML (force / recompute в API).
# В отладке результаты помечаются pipeline.debug и не используются как готовые
RISK_MONITORING_DEBUG = True

# Срок ссылок риск-мониторинга на общий документ: закрытие закупки/контракта + RM_TEXT_AFTER_CLOSE_DAYS,
# но не дольше RM_TEXT_MAX_RETENTION_DAYS от загрузки (решение 13.10.2026)
RM_TEXT_AFTER_CLOSE_DAYS = 365
RM_TEXT_MAX_RETENTION_DAYS = 1095

# ---------------------------------------------------------------- анализ закупки/контракта и организаций
# Зоны балла 0–100: меньше первого порога — зелёная, меньше второго — жёлтая, иначе красная
SCORE_ZONES = (40, 70)
# Подтверждённый аналитиком риск весит больше (status = 1 в risk_monitoring_document_risks)
CONFIRMED_WEIGHT = 1.5
# Балл: каждый следующий риск (по убыванию веса) весит в SCORE_DECAY раз меньше предыдущего — много средних рисков
# не дают «100»; риск, найденный только моделью и не подтверждённый аналитиком, весит AI_UNCONFIRMED_WEIGHT
SCORE_DECAY = 0.5
AI_UNCONFIRMED_WEIGHT = 0.6
# Источник риска для пользователя (колонка source): "simple" — «rule», если риск нашло правило (внешнее, наше по
# данным ЕИС, реестр, комплект, аналитика), в том числе вместе с моделью; «ai» — только модель. Подробный список —
# в sources. "detailed" — прежние значения (registry / rule / package / aggregate / ai_event / ai_file)
RISK_SOURCE_LABELS = "simple"
# Показатели внешней системы с известными ошибками в формуле: не перекрывают и не подтверждают выводы модели
# (после исправления правил разработчиком — пусто)
RULE_SUSPECT_INDICATORS: tuple = ()
# Перепроверка сработавших показателей по документам закупки: справочник исключений
# (risk_monitoring/config/rule_exclusions.json, согласован с методистом). Найденное исключение риск не снимает
# (концепция, 2.8: ИИ не заменяет правила; 1.6: решение — за аналитиком), а понижает уровень и уверенность
RULE_EXCLUSION_CHECK_ENABLED = True
RULE_EXCLUDED_LEVEL_FACTOR = 0.3     # уровень риска при найденном исключении × это значение
RULE_EXCLUDED_CONFIDENCE = 0.6       # уверенность риска при найденном исключении (не выше)
RULE_EXCLUSION_MAX_CHARS = 12000     # фрагментов документов в запросе к модели, символов
RULE_EXCLUSION_MAX_TOKENS = 4000     # лимит ответа модели при перепроверке правил (модели с «мышлением» тратят часть)

# ---------------------------------------------------------------- динамика риска (концепция, гл. 10–11)
# События риска (11.7): изменение силы риска (уровень × уверенность) на столько — повышение / снижение
RISK_CHANGE_THRESHOLD = 0.05
# Затухание (10.10): период полураспада риска, дней — по коду или категории; не указан — не затухает
# (ограничение конкуренции, нарушения исполнения, документы — до новой редакции, реестры — пока действует запись)
RISK_HALF_LIFE_DAYS: dict = {
    "FIN-013": 90,                                            # изменение НМЦК (пример концепции — 90 дней)
    "CTR-004": 90, "CTR-005": 90, "CTR-006": 90, "CTR-008": 90, "CTR-014": 90,   # изменения условий контракта
    "PROC-002": 90, "PROC-005": 90, "PROC-009": 90,           # изменение / отмена процедуры, заявки, уклонение
    "PROC-008": 180, "CTR-019": 180,                          # жалобы, претензии (концепция — 180 дней)
}
# Пересчёт затухания закупок, которых не касались новые документы (иначе их балл не снижается): раз в неделю
RISK_DECAY_RECALC_ENABLED = True
RISK_DECAY_RECALC_WEEKDAY = 1        # понедельник (crontab day_of_week)
RISK_DECAY_RECALC_HOUR = 4
RISK_DECAY_MIN_CHANGE = 1.0          # балл изменился меньше — не перезаписывать
# Пересчёт закупки после решения аналитика (05.11.2026): решения по всем строкам рисков (закупка, файлы, события,
# в том числе снятые отклонением) читаются из risk_monitoring_document_risks; закупки, где аналитик менял статус за
# RISK_DECISION_LOOKBACK_MIN минут (status_changed_at, время БД), пересчитываются без модели, если изменился отпечаток
# решений (risk_monitoring.risk_decisions)
RISK_DECISION_RECALC_ENABLED = True
RISK_DECISION_RECALC_EVERY_MIN = 2   # период задачи rm_decisions_task, минут
RISK_DECISION_LOOKBACK_MIN = 15      # окно поиска изменённых строк, минут (с запасом на пропущенный запуск)
# Одновременных PATCH ai_analysis во внешнюю БД при массовых пересчётах (затухание, решения аналитиков;
# risk_monitoring/batch_push.py). Частоту всё равно держит лимитер RiskMonitoringAPI (0.9 запроса/с — у API 60 в минуту)
RM_PUSH_CONCURRENCY = 3
# Предел тела PATCH ai_analysis во внешнюю БД, байт (risk_monitoring_api.RiskMonitoringAPI._patch_body). У API
# post_max_size=8M: тело больше не отправляется, ошибка сразу называет самые крупные части ai_analysis
RM_PUSH_MAX_BYTES = 7_500_000
# Приоритет проверки (10.14): веса составляющих (сумма 1); итог × (0,5 + 0,5 × доверие)
PRIORITY_WEIGHTS = {"risk": 0.4, "growth": 0.25, "money": 0.15, "novelty": 0.2}
# Уровень риска от сработавшего правила внешней системы (rule_values): признак риска («нет обеспечения заявки»,
# «аванс», «короткий срок» — срабатывают почти у каждой закупки) и нарушение. При 0,4 четыре типовых правила без
# единой находки в документах давали 59 баллов — 0 зелёных из 76 закупок (прогон 2026-07-03); решение 22.10.2026 — 0,2
RULE_RISK_LEVEL = 0.2
RULE_VIOLATION_LEVEL = 0.55
# Расхождение сумм между документом и ЕИС, при котором ставится DOC-009 (доля)
CONSISTENCY_TOLERANCE = 0.01
# Ставить ли DOC-015 «Неполный комплект документов» (иначе — только в package.completeness)
COMPLETENESS_RISK = True
# Резюме закупки и организации пишет модель (иначе — шаблон)
PACKAGE_LLM_RESUME = True

# Период и пороги аналитики организаций
ORG_PERIOD_MONTHS = 12
ORG_MIN_CONTRACTS = 5                  # меньше — поведенческие риски не ставятся (мало данных)
CUSTOMER_LOW_COMPETITION_SHARE = 0.5   # доля закупок с одной заявкой / у единственного поставщика → BEH-001
CUSTOMER_TOP_SUPPLIER_SHARE = 0.4      # доля контрактов заказчика у одного поставщика → BEH-006 «любимый поставщик»
CUSTOMER_HHI = 2500                    # индекс Херфиндаля–Хиршмана по суммам контрактов заказчика → BEH-005
SUPPLIER_TERMINATION_SHARE = 0.2       # доля расторгнутых контрактов поставщика → BEH-002
DEMPING_PCT = 25                       # снижение цены на торгах, %, с которого контракт считается демпинговым
REPEAT_RISK_MIN = 3                    # риск одного кода в стольких закупках заказчика → HIST-001
# ai_analysis организаций (секции customer / supplier) — в risk_monitoring_organisations.ai_analysis (ТЗ 09.10.2026)
ORG_PUSH_ENABLED = True
# Сводный показатель заказчика по «Модели сводной аналитики» (РАО, 2025, раздел 3–4):
#   Псвод = Пзак + Пис + Пприем;  Пблок = (Σ k_i × П_i) × 100 × Кблок
# Блоки: заключение (определение поставщика и заключение) Кзак = 0,2; реализация Кис = 0,5; приёмка Кприем = 0,3.
# П_i — доля закупок без нарушений по показателю. В модели П_i выставляет эксперт по форме заключения; в
# риск-мониторинге показатель закупки = 1 − интенсивность рисков каталога, отнесённых к показателю (нет рисков — 1).
# Чем выше Псвод, тем лучше: зелёная зона ≥ 85, жёлтая 70–85, красная < 70 (CUSTOMER_QUALITY_ZONES).
# В ai_analysis заказчика: quality_index = Псвод, score = 100 − Псвод (балл риска, как у закупок).
# "aggregate" — прежний балл только по поведенческим рискам заказчика
CUSTOMER_SCORE_MODEL = "quality"
CUSTOMER_QUALITY_ZONES = (70, 85)
CUSTOMER_QUALITY_BLOCKS = (
    {"name": "conclusion", "title": "заключение контрактов", "weight": 0.2, "applies": "all", "indicators": (
        ("P11", "обоснованность НМЦК", 0.20, ("FIN-001", "FIN-002", "FIN-013", "FIN-015")),
        ("P12", "обоснованность выбора поставщика", 0.25,
         ("PROC-001", "PROC-003", "PROC-005", "PROC-006", "PROC-007", "PROC-008", "PROC-009", "PROC-010", "PROC-012",
          "FIN-003", "REG-001", "REG-002", "BEH-006")),
        ("P13", "проработка описания объекта закупки", 0.25,
         ("DOC-002", "DOC-003", "DOC-005", "DOC-006", "AI-001", "AI-003", "AI-005", "PROC-011")),
        ("P14", "соответствие проекта контракта типовым условиям", 0.15,
         ("DOC-001", "FIN-004", "FIN-005", "FIN-006", "FIN-007", "FIN-008", "FIN-009", "FIN-012", "FIN-016",
          "CTR-001", "CTR-002", "CTR-003")),
        ("P15", "соответствие документации законодательству", 0.15,
         ("DOC-004", "DOC-007", "DOC-008", "DOC-009", "DOC-010", "DOC-015", "AI-002", "PROC-002", "PROC-004",
          "FIN-014")),
    )},
    # реализация и приёмка — только закупки с заключённым контрактом
    {"name": "execution", "title": "реализация контрактов", "weight": 0.5, "applies": "contract", "indicators": (
        ("P21", "отчётная документация (состав, сроки, форма)", 0.20, ("DATA-001",)),
        ("P22", "соблюдение сроков исполнения", 0.25, ("CTR-005", "CTR-010", "CTR-017")),
        ("P23", "соответствие результатов требованиям", 0.35,
         ("CTR-007", "CTR-008", "CTR-009", "CTR-015", "CTR-016", "CTR-018", "CTR-020")),
        ("P24", "соответствие исполнения и документов условиям контракта", 0.10,
         ("CTR-004", "CTR-006", "CTR-011", "CTR-012", "CTR-013", "CTR-014", "CTR-019")),
        ("P25", "привлечение субподрядчиков из СМП / СОНО", 0.10, ("CTR-021",)),
    )},
    {"name": "acceptance", "title": "приёмка контрактов", "weight": 0.3, "applies": "contract", "indicators": (
        ("P31", "соблюдение сроков приёмки", 0.20, ("CTR-024",)),
        ("P32", "привлечение независимой экспертной организации", 0.10, ()),
        ("P33", "невыставленные штрафы", 0.30, ()),
        ("P34", "невыставленные пени", 0.30, ("CTR-022", "CTR-023")),
        ("P35", "акт приёмки и оплата по нему", 0.10, ("FIN-010", "FIN-011")),
    )},
)
CUSTOMER_VIOLATION_WEIGHT = 1.5        # нарушение (вид violation) весит больше риска того же уровня (не больше 1)
# Поведенческие признаки заказчика (BEH, HIST, REG по совокупности закупок) в модели не учитываются; с весом > 0
# снижают Псвод: Псвод × (1 − w × балл_поведения / 100). По умолчанию 0 — как в модели
CUSTOMER_BEHAVIOUR_WEIGHT = 0.0

# ---------------------------------------------------------------- ежедневный прогон и сводка (S0–S4)
# Прогон за дату выгрузки запускает Laravel (POST /risk-monitoring/daily); если до DAILY_WATCHDOG_HOUR вызова не было,
# Celery beat запускает его сам для последней даты выгрузки окружения DAILY_ENVIRONMENT
DAILY_WATCHDOG_ENABLED = True
DAILY_WATCHDOG_HOUR = 6
DAILY_ENVIRONMENT = ENVIRONMENT         # окружение текущей установки (APP_ENV)
DAILY_PUSH = True                      # записывать результаты этапов во внешнюю БД
# Сводка — и в risk_monitoring_daily_runs (POST /api/risk-monitoring/daily-runs, ТЗ 09.10.2026); у нас — всегда
DAILY_RUNS_PUSH_ENABLED = True
# Профили поставщиков затронутых закупок: строка организации — POST /api/risk-monitoring/organisations
# (существующая или новая с is_external = 1, monitoring = 0), затем секция supplier
DAILY_SUPPLIERS_ENABLED = True
# Заказчик системы — Минобрнауки: все организации risk_monitoring_organisations (кроме is_external = 1 — поставщиков)
# — его подведомственные, и любая их закупка разбирается и анализируется. monitoring = 1 — только организации с
# текущими задачами мониторинга (≈ 121): в сводке они выделяются, но охват ими не ограничивается (23.10.2026).
# True — сводка, заказчики и поставщики только по monitoring = 1 (прежнее поведение)
DAILY_MONITORED_ONLY = False
DAILY_SCORE_JUMP = 15                  # рост балла закупки за прогон, с которого она попадает в «внимание»
DAILY_TOP = 15                         # сколько закупок и событий показывать в каждом списке сводки
DAILY_EVENT_IMPORTANCE = ("high", "critical")   # события этой важности попадают в «внимание»
# Охват анализа: "all" — все XML за дату; "own" — только XML, привязанные к закупкам системы (без «сирот»)
DAILY_SCOPE = "all"
# При DAILY_SCOPE = "all": если XML за дату больше — анализируются только XML закупок системы
# (массовая загрузка, 2026-07-03: 21 907 XML; обычная дата — 100–4000). None — без ограничения
DAILY_ALL_MAX_XML = 5000
# XML ЕИС больше 30 МБ (risk_monitoring.xml_events.MAX_XML_BYTES) разбираются облегчённо (parse_xml_lite): у одного
# родителя остаётся не больше XML_LITE_MAX_CHILDREN одноимённых элементов (позиции, характеристики), тексты
# длиннее XML_LITE_MAX_TEXT обрезаются, поля версий не сравниваются (урезанные списки дали бы ложные изменения) —
# сведения, правила и вложения остаются. 17.11.2026 извещение epNotificationEF2020 > 30 МБ не разбиралось совсем;
# полный разбор двух версий по 40 МБ — около 730 МБ памяти. Больше XML_LITE_MAX_BYTES — ошибка без разбора
XML_LITE_MAX_BYTES = 200 * 1024 * 1024
XML_LITE_MAX_CHILDREN = 200
XML_LITE_MAX_TEXT = 64 * 1024
# Модель разбирает события закупок системы и подведомственных организаций (XML привязан к закупке или привязкой
# найден заказчик из risk_monitoring_organisations с is_external = 0). XML чужих заказчиков из региональных
# архивов ЕИС описываются правилами и шаблоном, pipeline.llm.deferred — отложено (прогон 02.07.2026: 2965 из 2973
# XML без привязки, этап событий 5600 с, partial). Если закупка позже появится в системе, отложенные XML её
# разбираются моделью повторно
DAILY_EVENT_LLM_OWN_ONLY = True
# Предыдущие версии XML запрашиваются пачками по столько id (соединение таблицы XML с собой: пачка 500 на
# 2973 XML — 504 шлюза, 02.07.2026)
RM_PREVIOUS_CHUNK = 100
# Параллельность ежедневного прогона (по умолчанию — прежние значения). Узкое место — модель: файлов одновременно ×
# запросов риск-анализа на файл ≈ одновременных запросов к LLM (3 × 3 = 9). Поднимать вместе с возможностями сервера
# модели (vLLM: --max-num-seqs); события — в основном внешняя БД и правила, модель только для закупок системы
RM_FILE_CONCURRENCY = 3
# Открытых архивов ЕИС одновременно в кэше прогона (LRU): при всех открытых прогон за большой день падал на
# «Too many open files» (прод, 11.11.2026); закрытый архив открывается заново при обращении
RM_MAX_OPEN_ARCHIVES = 64
# ЕИС отвечает 403 «подозрительная активность» на частые запросы (18.11.2026: все файлы подряд): скачивание из ЕИС
# приостанавливается на столько секунд, файлы повторяются без траты попытки (ai_file_analyzer.SourceBlocked)
RM_EIS_BLOCK_PAUSE_SEC = 900
# Архив документов в РМ (configs/file_reader.FileReader.archive_limits): 18.11.2026 архив проектной документации
# «Т-15.part01.rar» (сотни смет, чертежи-картинки) читался больше 30 мин, дальше — сотни запросов извлечения.
# Читается не больше RM_ARCHIVE_MAX_FILES файлов (сначала документы, потом таблицы, картинки — последними, не больше
# RM_ARCHIVE_MAX_IMAGES на OCR), пока текст не превысит RM_ARCHIVE_MAX_CHARS; остальные — списком имён.
# Экспертиза читает архивы целиком (пределов нет); служебные файлы (~$*.docx, Thumbs.db, __MACOSX) пропускаются везде
RM_ARCHIVE_MAX_FILES = 40
RM_ARCHIVE_MAX_IMAGES = 10
RM_ARCHIVE_MAX_CHARS = 300_000
RM_ARCHIVE_CONCURRENCY = 4
RM_EVENT_CONCURRENCY = 3
RM_RISK_LLM_CONCURRENCY = 3
RM_RISK_LLM_RATE = 1.5          # запросов в секунду на экземпляр риск-анализа
# Извлечение данных файлов РМ (19.11.2026; замеры 17.11: извлечение моделью — 56 % времени файлов).
# Профиль полей по типу документа (evaluate_documents/doc_profiles.py): тип определяется до извлечения, модель
# заполняет только поля типа — ответ короче. False — полная схема и тип после извлечения, как раньше
RM_EXTRACTION_PROFILES = True
# Документы со стабильным форматом — кодом: XML-файлы (реквизиты по тегам, risk_monitoring/code_extraction.py) и
# платёжные поручения, разобранные полностью (без модели). False — всё моделью, как раньше
RM_CODE_EXTRACTION = True
# XML: сколько первых блоков текста всё же отдаётся модели (название, краткое описание, что не лежит в тегах);
# 0 — только код. Раньше — все блоки: «Электронный контракт.xml» — 67 запросов, 511 с
RM_XML_MODEL_CHUNKS = 1
# Предел длины risks[].description при записи во внешнюю БД (API: «не может превышать 16000», HTTP 422 — закупка
# не записывалась; прод 12–13.11.2026). Описание из нескольких находок сокращается по целым находкам со строкой
# «… ещё N находок» (полный список — в risks[].findings), иначе обрезается. Внутренний результат не меняется
RM_EXTERNAL_DESCRIPTION_MAX = 16000
DAILY_XML_BATCH = 200                  # XML в пачке: после пачки память под разобранные XML освобождается
# Время на файлы и события, с: дальше пачки не начинаются — прогон собирает закупки и сводку до предела задачи
# (rm_daily_task: 13 500 с) и помечается partial; повторный запуск за ту же дату продолжит с необработанных
DAILY_DOCUMENTS_BUDGET_SEC = 9000
# Пачка, начатая до исчерпания бюджета, прерывается, если не уложилась и в этот запас: 9000 + 2400 = 11 400 с —
# остаётся время на закупки, заказчиков и сводку до лимита задачи (13 500 с)
DAILY_BATCH_OVERRUN_SEC = 2400
# Предел этапов закупок, заказчиков и поставщиков от начала прогона, с: этап, не уложившийся в него, прерывается
# (partial), а прогон успевает записать сводку до лимита задачи (13 500 с; 600 с — на текст сводки и запись).
# Прод, 15.11.2026: прогон за 13.11 снимался лимитом посреди этапа «suppliers» — сводка не записывалась, failed
DAILY_STAGES_DEADLINE_SEC = 12900
# Незавершённый прогон (partial — не хватило времени, failed — упал) доделывается в тот же день, без ручного
# запуска: сразу после завершения задачи ставится продолжение за ту же дату (уже проанализированное пропускается).
# Страховка: каждые DAILY_RESUME_EVERY_MIN минут проверяются прогоны последних DAILY_RESUME_LOOKBACK_HOURS часов
# (например, задача снята при перезапуске контейнера и продолжение не поставилось)
DAILY_CONTINUE_ENABLED = True
DAILY_CONTINUE_DELAY_SEC = 60              # partial: продолжение через минуту
DAILY_RETRY_FAILED_DELAY_SEC = 900         # failed: повтор через 15 минут (сбой сети, перезапуск модели)
DAILY_CONTINUE_MAX_ATTEMPTS = 12           # запусков за одну дату — дальше только вручную (не крутиться бесконечно)
DAILY_RESUME_EVERY_MIN = 30
DAILY_RESUME_LOOKBACK_HOURS = 48
# Заказчики и поставщики (OrgAnalyzer: резюме моделью на каждую организацию) и текст сводки моделью — только в
# запуске, после которого продолжений за дату не будет (документы обработаны или исчерпан
# DAILY_CONTINUE_MAX_ATTEMPTS). Пока файлы / события partial, этапы customers / suppliers — deferred, в сводку
# переносятся блоки заказчиков и поставщиков прошлого запуска за ту же дату. 16.11.2026: каждое продолжение
# пересчитывало всех заказчиков и поставщиков заново — 549 + 799 с × 12 запусков за 12.11 (≈ 4,5 ч впустую).
# False — как раньше (каждый запуск)
DAILY_DEFER_ORGS_UNTIL_DOCS_DONE = True
# Одновременно идёт прогон только за одну дату (блокировка rm_daily:<env> в Postgres). 14–15.11.2026 прогоны за
# 13.11 и 14.11 шли параллельно и делили модель — медленнее оба. Запуск за другую дату, пока идёт прогон,
# откладывается на DAILY_WAIT_OTHER_DATE_SEC (новую дату — повтором задачи, незавершённую — страховкой
# rm_daily_resume_task). False — даты параллельно, как раньше
DAILY_ONE_DATE_AT_A_TIME = True
DAILY_WAIT_OTHER_DATE_SEC = 600
# Сначала доделывается более ранняя незавершённая дата (partial / failed / оборванная за
# DAILY_RESUME_LOOKBACK_HOURS часов, запусков меньше DAILY_CONTINUE_MAX_ATTEMPTS): запуск более поздней даты
# откладывается. Не действует при force (отладка)
DAILY_EARLIER_FIRST = True

# ---------------------------------------------------------------- типовые положения о закупке (223-ФЗ)
TYPAL_LOAD_ENABLED = True
# Коды регионов ФНС (две цифры); пусто — регионы заказчиков на мониторинге (dataset.owner.region)
TYPAL_REGIONS: tuple = ()
TYPAL_REGIONS_ENVIRONMENT = ENVIRONMENT  # окружение, по заказчикам которого определяются регионы (если TYPAL_REGIONS пуст) — APP_ENV
TYPAL_SUBSYSTEM = "TPOZ223"              # подсистема ЕИС для getDocsByOrgRegionRequest
TYPAL_DOCUMENT_TYPE = "typalOrderClause"
TYPAL_LOOKBACK_DAYS = 3                # ежедневная загрузка берёт столько прошедших дней (ЕИС выгружает с задержкой)
# Лимит ЕИС — 90 запросов в 60 с на адрес (общий с загрузчиком внешней системы): пауза между запросами и повтор
# после «Код 13» через минуту
TYPAL_EIS_MIN_INTERVAL_SEC = 1.5
TYPAL_EIS_LIMIT_WAIT_SEC = 61
TYPAL_EIS_LIMIT_RETRIES = 2

# Положения о закупке конкретных заказчиков (orderClause, POZ223): тот же запрос по региону и дате, что и типовые
ORDER_CLAUSE_LOAD_ENABLED = True
ORDER_CLAUSE_SUBSYSTEM = "POZ223"
ORDER_CLAUSE_DOCUMENT_TYPE = "orderClause"
# сохранять только положения заказчиков на мониторинге (иначе — все положения региона за день с чтением файлов)
ORDER_CLAUSE_ONLY_MONITORED = True

# Журнал воркеров Celery в файл (logs/celery_rm.log, logs/celery.log, logs/celery_beat.log) с ротацией: переживает
# пересоздание контейнера (вывод `docker logs` — нет)
WORKER_LOG_ENABLED = True
WORKER_LOG_DIR = "logs"
WORKER_LOG_MAX_MB = 100
WORKER_LOG_BACKUPS = 10
# Длинные значения в журнале уровня INFO (результаты извлечения, ответы OCR, разбор ссылок) обрезаются до стольких
# символов (configs.logger.brief); полный текст — на уровне DEBUG. 0 — писать целиком, как раньше
LOG_VALUE_MAX_CHARS = 2000
# Процессов воркера celery_worker (очередь evaluation, пул prefork): задачи /evaluate-documents, сводного заключения,
# индексации и публикаций экспертов идут параллельно. У каждого процесса свои лимиты запросов к модели
# (llm_resilience.MAX_INFLIGHT), поэтому нагрузка на модель растёт с числом процессов; 1 — задачи по одной.
# Воркер риск-мониторинга задаёт своё число процессов в docker-compose.yml (--concurrency=2) — оно важнее этой настройки
CELERY_EVAL_CONCURRENCY = 2

# Переменные, которые раньше задавались в .env и теперь задаются здесь (имя в .env → имя здесь, модуль).
# Если в окружении осталось другое значение, при старте пишется предупреждение (см. warn_env_leftovers)
MOVED_FROM_ENV = {
    "KNOWLEDGE_STORE_ENABLED": "KNOWLEDGE_STORE_ENABLED", "PE_FACTS_ENABLED": "PE_FACTS_ENABLED",
    "PE_TEXT_RETENTION_DAYS": "PE_TEXT_RETENTION_DAYS", "DOC_DEDUP_ENABLED": "DOC_DEDUP_ENABLED",
    "DOC_EXTRACTION_CACHE_ENABLED": "DOC_EXTRACTION_CACHE_ENABLED", "PE_RM_LINK_ENABLED": "PE_RM_LINK_ENABLED",
    "EVAL_LLM_CONCURRENCY": "EVAL_LLM_CONCURRENCY", "EVAL_FILE_CONCURRENCY": "EVAL_FILE_CONCURRENCY",
    "EVAL_LINK_CONCURRENCY": "EVAL_LINK_CONCURRENCY", "PE_LEGACY_ENV": "PE_LEGACY_ENV",
    "PE_CHUNKER": "PE_CHUNKER", "PE_MODEL_ZEROS": "PE_MODEL_ZEROS", "PE_FACTS_TIMEOUT_SEC": "PE_FACTS_TIMEOUT_SEC",
    "PE_FACTS_TOP_K": "PE_FACTS_TOP_K", "PE_FACTS_RECHECK_K": "PE_FACTS_RECHECK_K",
    "MODEL_TEMPERATURE": "MODEL_TEMPERATURE", "MODEL_MAX_TOKENS": "MODEL_MAX_TOKENS",
    "BATCH_SIZE": "EMBEDDING_BATCH_SIZE", "RETRY_MAX_ATTEMPTS": "RETRY_MAX_ATTEMPTS", "RETRY_DELAY": "RETRY_DELAY",
    "RETRY_BACKOFF": "RETRY_BACKOFF",
}
RM_MOVED_FROM_ENV = ("RISK_MONITORING_DEBUG", "RM_TEXT_AFTER_CLOSE_DAYS", "RM_TEXT_MAX_RETENTION_DAYS",
                     "RM_BOILERPLATE", "RM_LLM_STREAM")


def _same(env_value: str, value) -> bool:
    """Совпадает ли строка из окружения со значением из кода (с учётом типа)."""
    s = env_value.strip()
    if isinstance(value, bool):
        return s.lower() == str(value).lower()
    if isinstance(value, (int, float)):
        try:
            return float(s) == float(value)
        except ValueError:
            return False
    return s.lower() == str(value).lower()


def env_leftovers() -> list:
    """Переменные .env, перенесённые в код, у которых в окружении другое значение.

    Returns:
        list: ``[(имя в .env, значение в .env, значение в коде)]`` — значение в окружении игнорируется.
    """
    out = []
    for env_name, name in MOVED_FROM_ENV.items():
        v = os.environ.get(env_name)
        if v is not None and not _same(v, globals()[name]):
            out.append((env_name, v, globals()[name]))
    for name in RM_MOVED_FROM_ENV:
        v = os.environ.get(name)
        if v is not None and name in globals() and not _same(v, globals()[name]):
            out.append((name, v, globals()[name]))
    return out


def warn_env_leftovers(logger) -> None:
    """Пишет предупреждение для каждой переменной из :func:`env_leftovers` (вызывается при старте приложения)."""
    for env_name, env_value, code_value in env_leftovers():
        logger.warning(f"В .env задано {env_name}={env_value}, но значение теперь задаётся в коде ({code_value}): "
                       f"переменная игнорируется — перенесите значение в settings.py или удалите строку из .env")

# ── Составные риски закупки (CR-*): сочетания факторов, шаблоны — risk_monitoring/config/composite_risks.json
COMPOSITE_RISKS_ENABLED = True

# ── Профили закупки (risk_monitoring/purchase_profiles.py)
# Исполнение контракта: уровень риска просрочки (CTR-017) по числу дней — (до N дней включительно, уровень)
EXECUTION_DELAY_LEVELS = ((7, 0.3), (30, 0.45), (10**6, 0.6))
EXECUTION_UNDERDONE_SHARE = 0.9      # завершён, а исполнено меньше этой доли цены → CTR-018
# Сравнение с похожими закупками системы (класс ОКПД2 + закон + конкурентный способ)
BENCHMARK_ENABLED = True
BENCHMARK_LOOKBACK_DAYS = 730        # период похожих закупок
BENCHMARK_MIN_PEERS = 10             # меньше — раздел ОКПД2, ещё меньше — не сравнивать
BENCHMARK_MIN_MEDIAN = 8.0           # медиана снижения у похожих, %, ниже которой малое снижение не аномально
BENCHMARK_LOW_DISCOUNT_GAP = 5.0     # насколько п. п. снижение должно быть меньше медианы
BENCHMARK_RISK_LEVEL = 0.4           # уровень FIN-001 (для раздела ОКПД2 — × 0,75)

# ── Сводки за неделю и месяц (risk_monitoring/period_summary.py): по расписанию — ежедневно в это время
# (прошлая неделя и прошлый месяц; без изменений ежедневных сводок не пересобираются)
PERIOD_SUMMARY_ENABLED = True
PERIOD_SUMMARY_HOUR = 11
PERIOD_SUMMARY_MINUTE = 23
# Запись сводок периода в risk_monitoring_daily_runs (period_type = week / month). Включено 03.11.2026: требует миграции
# таблицы (period_type, period_key, UNIQUE (period_type, export_date)) и доработки POST /api/risk-monitoring/daily-runs
PERIOD_SUMMARY_PUSH_ENABLED = True

# ── Рассуждение модели (configs/llm_resilience.py): prod_multimodal_llm тратила весь лимит ответа на рассуждение и
# возвращала пустой ответ (02.11.2026). Выключаем рассуждение (chat_template_kwargs.enable_thinking = false); если
# сервер его не выключает — один повтор с бо́льшим лимитом ответа
LLM_DISABLE_THINKING = True
# Ответ модели потоком (stream=True), собирается в обычный ответ (configs/llm_resilience.py): шлюз llmhub рвёт
# запрос, если ответ не начался за 90 с (504), а длинное извлечение (до 4000 токенов при 45 ток/с под нагрузкой)
# генерируется дольше — проверено scripts/llm_probe.py 15.11.2026. В потоке данные идут сразу, и шлюз не ждёт
LLM_STREAM_RESPONSES = True
# Длинный system-промпт переносится в начало первого сообщения user (configs/llm_resilience.py). 15.11.2026
# prod_multimodal_llm на промпте извлечения (19 КБ, ~3800 токенов) в роли system отвечал «{ Register Register …»
# (36 ток/с), тот же текст в сообщении user — нормальный JSON (165 ток/с); scripts/llm_probe.py --isolate.
# Порог 8000 не защитил: в 21:05 мусор давала уже первая половина промпта (~9 КБ), а вторая — нет, то есть
# дело не в длине. Поэтому переносятся все system-сообщения (1 — любой длины). 0 — не переносить.
# 16.11.2026 17:05 кэш префиксов сочли починенным и выключили перенос — к 18:20 мусор вернулся, перенос снова включён
LLM_SYSTEM_AS_USER_CHARS = 1
# Уникальная строка в начале каждого запроса к модели (configs/llm_resilience._nonce_prefix): обход испорченного
# кэша префиксов сервера (vLLM prefix caching). 15.11.2026 prod_multimodal_llm давал мусор («Register», «duct»)
# только на уже отправлявшихся приложением началах запросов — в любой роли и с любой схемой; те же промпты с новым
# началом (половина, без каталога) — норма (scripts/llm_probe.py --risk). Цена — сервер заново считает промпт
# (~0,3 с на 4 тыс. токенов). False — после починки сервера.
# 16.11.2026 17:05 кэш префиксов сочли починенным и выключили обход — к 18:20 мусор вернулся (кэш снова
# портится после того, как начала запросов накапливаются в нём), обход снова включён. Выключать только после
# того, как scripts/llm_probe.py --risk (R1, R4) несколько часов подряд даёт JSON при обычной нагрузке
LLM_PREFIX_NONCE = True
# Обрыв зацикливания в потоке (configs/llm_resilience.py): каждые LLM_LOOP_STEP_CHARS символов ответа последние
# LLM_LOOP_WINDOW_CHARS сжимаются zlib; степень сжатия ≥ LLM_LOOP_RATIO два раза подряд — модель повторяет одно и
# то же («Register Register …», одна запись списка десятки раз): генерация обрывается, запрос повторяется один раз
# со штрафом за повторы. Обычный JSON извлечения сжимается в 4–6 раз, повтор записи — в 12+, «Register …» — в 70
LLM_LOOP_CUT = True
LLM_LOOP_WINDOW_CHARS = 3000
LLM_LOOP_STEP_CHARS = 1000
LLM_LOOP_RATIO = 12.0
LLM_THINKING_RETRY_MAX_TOKENS = 12000

# ── Лимиты запросов к модели (configs/rate_limiter.py, configs/llm_resilience.py)
# Частота запросов к модели на процесс — один бак на всех (раньше TokenBucket(rate=1.5) был у каждого объекта, и общая
# частота не ограничивалась). 0 — без лимита частоты; одновременность ограничивает llm_resilience.MAX_INFLIGHT
LLM_RATE_PER_SEC = 3.0

# ── Чтение Excel (configs/file_reader.py): потоково и с ограничениями — полная загрузка книги-сметы подвешивала
# прогон (31.10.2026)
EXCEL_MAX_ROWS = 300        # непустых строк на лист
EXCEL_MAX_COLS = 60         # столбцов
EXCEL_MAX_SHEETS = 20       # листов
EXCEL_MAX_CHARS = 60_000    # символов всего (~20 тыс. токенов): смета на 300 тыс. символов — десятки запросов извлечения

# ── Жалобы в профилях организаций (заявитель раскрывается только в жалобе; участники процедур в ЕИС скрыты)
CUSTOMER_COMPLAINTS_JUSTIFIED_MIN = 2   # обоснованных жалоб на закупки заказчика за период → BEH-001
SUPPLIER_COMPLAINTS_MIN = 5             # жалоб поставщика за период, с которых оценивается их обоснованность
SUPPLIER_UNFOUNDED_SHARE = 0.7          # доля необоснованных среди рассмотренных → BEH-002

# ── Внеплановые проверки контрольных органов (risk_monitoring/control_checks.py)
CHECK_LOOKBACK_DAYS = 365           # проверки за этот период учитываются в оценке закупок
# уровень PROC-008 закупки из проверки с нарушениями: проверяли самого заказчика / организацию, проводившую закупку
CHECK_RISK_LEVELS = {"customer": 0.7, "other": 0.3}
CHECK_PRESCRIPTION_BONUS = 0.1      # выдано предписание
CUSTOMER_CHECK_VIOLATIONS_MIN = 1   # проверок заказчика с нарушениями → BEH-001

# ── Оформление заключений ИИ (risk_monitoring/text_format.py): "plain" — абзацы и списки «— » без символов разметки
# (читается и как простой текст, и как markdown); "markdown" — для интерфейса, который рендерит markdown
AI_TEXT_FORMAT = "plain"
# Определение типа документа (TypeDetector.detect): сколько символов начала документа отдаётся модели
TYPE_DETECT_HEAD_CHARS = 8000
# Предел ответа модели при определении типа, токенов: ответ — короткий JSON (тип, уверенность, признаки), а при
# зацикливании модель генерировала до прежних 3000 токенов («Register Register …», 27 тыс. символов за 372 с,
# прод 15.11.2026). Негодный ответ отбрасывается, тип — по имени файла
TYPE_DETECT_MAX_TOKENS = 1000

# Оценка первичных экспертов (knowledge_store/primary_compare.py). Взвешенное число расхождений с итоговым сводным
# заключением: (пропущенное нарушение, ложное нарушение, 1 против 2). У закупок пропуск нарушения весит втрое больше
# (на истории точность оценки 43,5% → 47,9%); у отчётных документов вес не помог (41% против 45%), поэтому 1:1:1.
# Пороги: счёт меньше первого → 1.00, меньше второго → 0.75, меньше третьего → 0.50, иначе 0.00.
# Подобраны по истории (scripts/eval_primary_opinion.py --compare) на тех же данных; оценка — ориентир.
PRIMARY_REVIEW_WEIGHTS = {"procurement": (3, 1, 1), "report": (1, 1, 1)}
PRIMARY_REVIEW_THRESHOLDS = {"procurement": (13, 21, 39), "report": (2, 3, 10)}
# Замечание «в перечне не указаны документы» — только если не указано не меньше стольких документов: на истории при >=3
# тема встречается в комментариях сводных экспертов в 43% случаев (при >=1 — в 27%, в среднем — в 18%)
PRIMARY_REVIEW_DOCS_MISSING_MIN = 3
# Постпроверка «1.00 только при полном соответствии» не применяется, если взвешенный процент сходства с итоговым выше этого
# значения (без взвешенного — обычный match_percent). Без исключения постпроверка снижает точность на закупках 59% → 44%,
# с исключением >95 — 58% (верных среди выданных 1.00 около 76%)
PRIMARY_REVIEW_FULL_MATCH_EXEMPT_PERCENT = 95
# Основной способ оценки: взвешенный процент сходства с итоговым (веса критериев и пороги процента выучены по истории,
# knowledge_store/primary_score_model.json). False — многоклассовая модель primary_range_model.json (запасная, сравнение)
PRIMARY_REVIEW_USE_WEIGHTED_PERCENT = True
PRIMARY_REVIEW_PERCENT_MARGIN = 2.0   # п. п. до ближайшего порога, в пределах которых показывается вторая оценка (alternative_range)
# Отбор расхождений в комментарий по вероятности, что сводный эксперт упомянет критерий (primary_mention_model.json, закупки);
# False — в комментарий идут все расхождения, поле mention_probability в ответе остаётся
PRIMARY_REVIEW_MENTION_FILTER = True
# Замечания модели к тексту заключения (запрос with_llm=true): какие категории попадают в ответ и комментарий. По проверке на истории
# (100 заключений) лучше базовой доли темы только «Формальные недостатки» (точность 47% против 37%); аргументация (4% против 4%) и
# противоречия (14% против 15%) базовую долю не превзошли — выключены
PRIMARY_REVIEW_LLM_CATEGORIES = ("FORMAL",)
PRIMARY_REVIEW_MIN_COMMON = 5   # меньше общих критериев — сравнение ненадёжно, оценка не предлагается

# Шкала аналитика: процент → оценка. Верхняя граница включается: до 25% включительно — 0, до 50% — 0.5, до 75% — 0.75, выше — 1.
# Что именно выражено процентом, аналитик пока не уточнил; в ответе API он считается как доля критериев, по которым
# первичный эксперт совпал с итоговым заключением (поле match_percent), и даёт оценку range_by_scale.
PRIMARY_REVIEW_SCALE = ((25, 0.0), (50, 0.5), (75, 0.75))
PRIMARY_REVIEW_SCALE_TOP = 1.0

# Тестовые пользователи (по users.csv от 17.11.2026; кандидатов ищет ``python -m scripts.find_test_experts``).
# Их заключения и оценки не используются для обучения и оценки модели первичных экспертов
# (scripts/fit_primary_range.py, scripts/eval_primary_opinion.py):
# * 2, 8, 75, 203, 977 — имена «test», «Тестовый» и т. п., без заключений;
# * 3, 4 — тестовые первичные эксперты (27 и 34 заключения, в 23 из них слово «тест»);
# * 1638 — тестовый сводный эксперт («Елена Тест РАО»);
# * 1538 (5 из 7 заключений со словом «тест») и 1806 (сводный, 1 из 3) — исключены по указанию;
# * 1584 — «Ковалёва gggg…», без заключений.
# Эксперт 7 (заглушки вместо реквизитов, но 28 правдоподобных заключений и 42 оценки) оставлен: решение 17.11.2026.
PRIMARY_REVIEW_TEST_EXPERT_IDS = frozenset({2, 3, 4, 8, 75, 203, 977, 1538, 1584, 1638, 1806})

# Постпроверка оценки первичного эксперта по порядку включения в Реестр: 1 балл — только при полном соответствии сводному
# заключению. «any» — любое расхождение (кроме общих для всех первичных) снижает 1.00 до 0.75; «missfa» — только пропуск
# нарушения и лишнее нарушение; «missed» — только пропуск; None — выключено. На истории (3,5 тыс. закупок): точность оценки
# 58,6% без правила и 43,3% с «any», зато доля верных среди выданных 1.00 растёт с 67% до 84%, а попадание в пределах
# одной ступени — с 92,6% до 95,8% (оценивающие на практике ставят 1.00 и при отклонениях).
PRIMARY_REVIEW_FULL_MATCH_RULE = "any"

# ── Доставка результата оценки первичных экспертов в портал (conclusion/primary_review_delivery.py), по согласованию с
# разработчиками РАО (РАО-ОЦЕНКА-2011-01). Адрес = SYSTEM_URL_<среда> + путь; ключ — Config.PRIMARY_REVIEW_API_KEY
PRIMARY_REVIEW_DELIVERY_PATH = "/api/expertise/set-hints-for-primary-experts"
# Окно доставки с момента запуска расчёта (портал принимает первую доставку в течение 10 минут), секунд
PRIMARY_REVIEW_DELIVERY_WINDOW = 600
# Паузы между повторами при 429, 5xx и обрыве связи, секунд (сумма меньше окна); тот же результат и тот же UUID
PRIMARY_REVIEW_DELIVERY_RETRY_DELAYS = (5, 15, 45, 120, 300)
# Таймаут одной попытки доставки, секунд
PRIMARY_REVIEW_DELIVERY_TIMEOUT = 60
# Предел тела запроса приёмника, байт (1 МиБ)
PRIMARY_REVIEW_DELIVERY_MAX_BYTES = 1024 * 1024

# Названия устаревших ключей полей, которых нет в справочнике текущих форм (остались в данных заявок от старых форм).
# Используются в ответе оценки первичных экспертов, когда справочник названия не знает (knowledge_store/primary_compare.label_for).
# field2_2_24: в «Отчеты и закупки поля.xlsx» критерий «2.2.7. Соответствие законодательству расчета НМЦК» разнесён по методам
# (field2_2_20–23 — нормативный, затратный, тарифный, проектно-сметный); 24-го ключа нет — это общий критерий старой формы
PRIMARY_REVIEW_LEGACY_LABELS = {
    "field2_2_24": "2.2.7. Соответствие законодательству расчета НМЦК",
}

# Отчётные документы (объект 7): не-критериальные поля сравниваются отдельно от критериев и в оценку не входят — только в
# комментарий (категория «Фактические ошибки»). Критерии — флаги field3_*/field4_* (primary_compare.comparable).
# 1) Анкетные факты (ответ «да/нет» верен либо неверен; не нарушение): требование субподряда из СМП (работы, НИР),
#    «требуется расчёт неустойки». Служебные поля не сравниваются: автозаполнение калькулятора (field*_1_8_0), ручной режим
#    (field*_1_12), решение эксперта «произвести расчёт фактически поставленного» (field5_2_0/field6_2_0) и скрытый флаг
#    «заполнить автоматически» (field5_0_1 в товарах, field6_0_1 в работах и НИР: до 12.2025 всегда True, с 01.2026 всегда
#    False — значение зависит от версии формы, а не от ответа эксперта).
PRIMARY_REVIEW_FACT_FIELDS = {
    "field5_0": "Есть ли в контракте требования к привлечению субподрядчиков из числа СМП",
    # работы и НИР (новая форма): «Является ли исполнитель … СМП?», выводится только при field5_0 = да; в товарах тот же ключ —
    # скрытый флаг (см. выше), поэтому сравнивается только при наличии маркера формы работ/НИР (PRIMARY_REVIEW_FACT_REQUIRES)
    "field5_0_1": "Является ли исполнитель по контракту/договору субъектом малого предпринимательства?",
    "field5_1_11": "Требуется расчет неустойки (штрафов и пеней)?",
    "field6_1_11": "Требуется расчет неустойки (штрафов и пеней)?",
}
# Условия сравнения факта: ключ → маркер формы (ключ, который должен быть в обоих заключениях) и родительское поле (факт
# сравнивается, только если родитель у обоих заключений «да»: иначе вопрос в форме не показывается).
PRIMARY_REVIEW_FACT_REQUIRES = {"field5_0_1": "field6_0_0"}
PRIMARY_REVIEW_FACT_PARENT = {"field5_0_1": "field5_0"}
# 2) Суммы калькулятора неустойки: название → ключи (товары field5_1_*, работы/НИР field6_1_*; суммы, выставленные
#    заказчиком, лежат в field6_1_4_1 / field6_1_10_1, в старых формах товаров — в field5_1_4_1 / field5_1_10_1).
PRIMARY_REVIEW_PENALTY_FIELDS = {
    "пени": ("field5_1_4", "field6_1_4"),
    "штрафы по стоимостным замечаниям": ("field5_1_7", "field6_1_7"),
    "штрафы по нестоимостным замечаниям": ("field5_1_10", "field6_1_10"),
    "сумма пеней, выставленная заказчиком": ("field6_1_4_1", "field5_1_4_1"),
    "сумма штрафов, выставленных заказчиком": ("field6_1_10_1", "field5_1_10_1"),
}
PRIMARY_REVIEW_PENALTY_TOLERANCE = 1.0   # руб.: допуск при сравнении сумм (округление калькулятора)

# Влияние замечаний к заключению первичного эксперта (issues, не расхождения по критериям) на предлагаемую оценку.
# Не ступень за категорию, а вычет из взвешенного процента сходства: влияют только серьёзные замечания (severity=major),
# замечания одного кода считаются один раз, сумма вычетов не больше PRIMARY_REVIEW_ISSUE_MAX_PERCENT, а оценка снижается
# (не более чем на ступень), только если процент после вычета ушёл ниже порога ступени, то есть замечание «добивает»
# пограничную оценку. Мелкие замечания и замечания модели к тексту идут только в комментарий. «Пропуски нарушений» и
# «Несоответствия» сюда не входят: они уже учтены во взвешенном проценте. Прежняя оценка — в ``range_before_issues`` и
# ``alternative_range``. Выключить: PRIMARY_REVIEW_ISSUE_PERCENT_DEFAULT = 0 и PRIMARY_REVIEW_ISSUE_PERCENT = {}.
# Первая версия (ступень за категорию) в бэктесте 24.11 снижала совпадение с оценками РАО: отчётные 53,2% → 48,9%, закупки
# 58,4% → 56,8%; веса ниже подлежат подбору по бэктесту (scripts/fit_primary_score.py).
PRIMARY_REVIEW_ISSUE_PERCENT = {
    "FACT_MISMATCH": 3.0,       # неверный анкетный факт
    "PENALTY_MISMATCH": 3.0,    # неверная сумма неустойки
    "METHOD_MISMATCH": 3.0,     # метод обоснования НМЦК выбран иначе
}
PRIMARY_REVIEW_ISSUE_PERCENT_DEFAULT = 2.0   # прочие серьёзные замечания (не заполнен перечень документов, пустой вывод и т. п.)
PRIMARY_REVIEW_ISSUE_MAX_PERCENT = 5.0       # п. п.: предел суммарного вычета

# Исправления названий критериев форм отчётных документов, по которым справочник (pe_form_fields, загружен из «Отчеты и закупки
# поля.xlsx») ошибочен: ключ → название. Ключ — суффикс кода формы. В листе «Поставка товаров» у блока 4.4 (место поставки) стоит
# ключ field4_4 вместо field4_3, а у блока 4.5.1 (сроки передачи) — повтор field4_4; по данным заключений field4_3 — место
# поставки, field4_4 — сроки передачи (так же устроены поля элементов field1_3: field4_3.q*, field4_4.q4–q7). Удалить после
# исправления листа и перезагрузки справочника.
PRIMARY_REVIEW_LABEL_OVERRIDES = {
    "goods_obj7": {
        "field4_3": "Итог по критерию: Подраздел 4.4. По критерию оценки «Соответствие места поставки товаров установленным требованиям»",
        # Название 4.5.1 — как в интерфейсе портала (подтверждено заказчиком справочника)
        "field4_4": "Итог по критерию: Подраздел 4.5.1 По критерию оценки «Сроки исполнения поставщиком обязательств по контракту/договору»",
    },
}

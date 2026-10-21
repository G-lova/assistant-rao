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

# Окружение установки из APP_ENV (.env; не задан — stage): development → dev, staging → stage, production → prod,
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
    return ENV_ALIASES.get(v, v or "stage")


try:  # APP_ENV может быть только в файле .env (вне docker), а settings импортируется раньше Config
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass
ENVIRONMENT = normalize_env(os.getenv("APP_ENV", "stage"))


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
# Сопоставление экспертизы с карточкой закупки риск-мониторинга после индексации (запросы к внешней БД)
PE_RM_LINK_ENABLED = True
# Параллельность /evaluate-documents на один вызов: запросы к LLM, файлы (чтение/OCR + извлечение), скачивание ссылок
EVAL_LLM_CONCURRENCY = 3
EVAL_FILE_CONCURRENCY = 3
EVAL_LINK_CONCURRENCY = 2
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

# Параметры генерации основной модели (MODEL_NAME)
MODEL_TEMPERATURE = 0.7
MODEL_MAX_TOKENS = 4096
# Размер пакета запроса эмбеддингов
EMBEDDING_BATCH_SIZE = 16

# Повторы HTTP-запросов (async_retry): попыток, первая задержка (с), множитель задержки
RETRY_MAX_ATTEMPTS = 3
RETRY_DELAY = 1.0
RETRY_BACKOFF = 2.0

# ================================================================ Риск-мониторинг

# Фильтр типовых формулировок (boilerplate.py): снимать риск, если цитата встречается у стольких разных
# заказчиков (закупки РМ и экспертизы)
BOILERPLATE_ENABLED = True
BOILERPLATE_MIN_SOURCES = 3

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
# Внешняя система пока не принимает ai_analysis организаций (нет таблицы в API): результат хранится у нас
ORG_PUSH_ENABLED = False

# ---------------------------------------------------------------- ежедневный прогон и сводка (S0–S4)
# Прогон за дату выгрузки запускает Laravel (POST /risk-monitoring/daily); если до DAILY_WATCHDOG_HOUR вызова не было,
# Celery beat запускает его сам для последней даты выгрузки окружения DAILY_ENVIRONMENT
DAILY_WATCHDOG_ENABLED = True
DAILY_WATCHDOG_HOUR = 6
DAILY_ENVIRONMENT = ENVIRONMENT         # окружение текущей установки (APP_ENV)
DAILY_PUSH = True                      # записывать результаты этапов во внешнюю БД
# Сводка — только по заказчикам с risk_monitoring_organisations.monitoring = 1
DAILY_MONITORED_ONLY = True
DAILY_SCORE_JUMP = 15                  # рост балла закупки за прогон, с которого она попадает в «внимание»
DAILY_TOP = 15                         # сколько закупок и событий показывать в каждом списке сводки
DAILY_EVENT_IMPORTANCE = ("high", "critical")   # события этой важности попадают в «внимание»
# Охват анализа: "all" — все XML за дату (как в плане), "monitored" — только XML закупок заказчиков на мониторинге
# (XML без привязки к закупке тогда не анализируются). Сводка в обоих случаях — по заказчикам на мониторинге
DAILY_SCOPE = "all"
DAILY_XML_BATCH = 500                  # XML в пачке: после пачки память под разобранные XML освобождается

# ---------------------------------------------------------------- типовые положения о закупке (223-ФЗ)
TYPAL_LOAD_ENABLED = True
# Коды регионов ФНС (две цифры); пусто — регионы заказчиков на мониторинге (dataset.owner.region)
TYPAL_REGIONS: tuple = ()
TYPAL_REGIONS_ENVIRONMENT = ENVIRONMENT  # окружение, по заказчикам которого определяются регионы (если TYPAL_REGIONS пуст) — APP_ENV
TYPAL_SUBSYSTEM = "TPOZ223"              # подсистема ЕИС для getDocsByOrgRegionRequest
TYPAL_DOCUMENT_TYPE = "typalOrderClause"
TYPAL_LOOKBACK_DAYS = 3                # ежедневная загрузка берёт столько прошедших дней (ЕИС выгружает с задержкой)

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

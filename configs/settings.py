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

# Риски модели в паспорте файла по кодам, которые внешняя система считает правилом (FIN-004…008, CTR-002 …), не
# ставятся: решает правило по данным ЕИС (в закупке — drop_rule_covered)
FILE_DROP_RULE_COVERED = True

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
# Модель разбирает события закупок системы и подведомственных организаций (XML привязан к закупке или привязкой
# найден заказчик из risk_monitoring_organisations с is_external = 0). XML чужих заказчиков из региональных
# архивов ЕИС описываются правилами и шаблоном, pipeline.llm.deferred — отложено (прогон 02.07.2026: 2965 из 2973
# XML без привязки, этап событий 5600 с, partial). Если закупка позже появится в системе, отложенные XML её
# разбираются моделью повторно
DAILY_EVENT_LLM_OWN_ONLY = True
# Предыдущие версии XML запрашиваются пачками по столько id (соединение таблицы XML с собой: пачка 500 на
# 2973 XML — 504 шлюза, 02.07.2026)
RM_PREVIOUS_CHUNK = 100
DAILY_XML_BATCH = 200                  # XML в пачке: после пачки память под разобранные XML освобождается
# Время на файлы и события, с: дальше пачки не начинаются — прогон собирает закупки и сводку до предела задачи
# (rm_daily_task: 13 500 с) и помечается partial; повторный запуск за ту же дату продолжит с необработанных
DAILY_DOCUMENTS_BUDGET_SEC = 9000
# Пачка, начатая до исчерпания бюджета, прерывается, если не уложилась и в этот запас: 9000 + 2400 = 11 400 с —
# остаётся время на закупки, заказчиков и сводку до лимита задачи (13 500 с)
DAILY_BATCH_OVERRUN_SEC = 2400
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

# Журнал воркеров Celery в файл (logs/celery_rm.log, logs/celery.log, logs/celery_beat.log) с ротацией: переживает
# пересоздание контейнера (вывод `docker logs` — нет)
WORKER_LOG_ENABLED = True
WORKER_LOG_DIR = "logs"
WORKER_LOG_MAX_MB = 100
WORKER_LOG_BACKUPS = 10

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

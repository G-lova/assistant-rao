"""Переключатели и лимиты сервиса — в коде, а не в ``.env``.

В ``.env`` остаются только адреса, ключи, пароли и названия моделей (они разные на dev / stage / prod).
Всё, что меняет поведение сервиса, задаётся здесь и попадает в историю репозитория вместе с кодом.
Переключатели риск-мониторинга — в ``risk_monitoring/settings.py``.

Значения читаются при каждом вызове (``settings.X``), поэтому в тестах их можно подменять
``mock.patch.object(settings, "X", ...)``.
"""

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
# Окружение строк, записанных до появления env в ключах (миграции 004, 008), и вызовов без X-API-Database.
# Зафиксировано для текущей установки: исторические данные — со prod
PE_LEGACY_ENV = "prod"

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
    import os

    from risk_monitoring import settings as rm

    out = []
    for env_name, name in MOVED_FROM_ENV.items():
        v = os.environ.get(env_name)
        if v is not None and not _same(v, globals()[name]):
            out.append((env_name, v, globals()[name]))
    for name in RM_MOVED_FROM_ENV:
        v = os.environ.get(name)
        if v is not None and hasattr(rm, name) and not _same(v, getattr(rm, name)):
            out.append((name, v, getattr(rm, name)))
    return out


def warn_env_leftovers(logger) -> None:
    """Пишет предупреждение для каждой переменной из :func:`env_leftovers` (вызывается при старте приложения)."""
    for env_name, env_value, code_value in env_leftovers():
        logger.warning(f"В .env задано {env_name}={env_value}, но значение теперь задаётся в коде ({code_value}): "
                       f"переменная игнорируется — перенесите значение в settings.py или удалите строку из .env")

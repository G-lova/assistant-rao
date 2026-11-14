import asyncio
from configs.http_client_manager import HTTPClientManager

from celery import current_task
from celery_app import LONG_TASK_LIMITS, celery_app

from configs.logger import get_logger
from evaluate_documents.evaluate_documents_pipeline import TasksPipeline
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


logger = get_logger(__name__)
        

def run_async(coro):
    """Запускает корутину с правильным управлением event loop"""
    return asyncio.run(coro)


@celery_app.task(bind=True, name="evaluate_documents_task", **LONG_TASK_LIMITS)
def evaluate_documents_task(self, expertise_id: int, environment: str, force: bool = False):
    """Оценка комплекта документов экспертизы (``/evaluate-documents``).

    Скачивает и читает документы, определяет их тип, извлекает данные, оценивает полноту и согласованность
    и сохраняет итог в ``raw_document_data``.

    Args:
        expertise_id: ID экспертизы.
        environment: Окружение (``X-API-Database``).
        force: Не использовать сохранённые текст и результат извлечения общего документа (перечитать файлы).

    Returns:
        dict: Итоговый отчёт ``ConsistencyChecker`` (формат ответа ``/task/{id}`` не меняется).
    """
    from celery_app import already_succeeded

    if already_succeeded(self):
        return self.AsyncResult(self.request.id).result

    # ✅ Инициализируем менеджер внутри асинхронного контекста задачи

    # @async_retry(CLOUD_PARSING_RETRY_CONFIG)
    async def run_with_manager():
        from knowledge_store.db import task_pool

        # один пул Postgres на задачу для хранилища знаний (создаётся лениво, только если оно включено)
        async with HTTPClientManager(timeout=90.0) as mgr, task_pool():
            tasks_piplene = await TasksPipeline.create(mgr, expertise_id, environment, force=force)
            task_result = await tasks_piplene.run_pipeline()
            return task_result
        
    async def run_with_retry():
        """Повтор конвейера при сбоях загрузки, но не при перегрузке модели.

        Раньше весь конвейер (скачивание, OCR, извлечение, все запросы к модели) запускался заново до 3 раз
        и на 504 шлюза модели — это утраивало нагрузку на перегруженный сервер. Теперь сбой сервера модели
        (5xx, таймаут) повторяется точечно внутри вызова (``configs.llm_resilience``), а конвейер целиком — нет.
        """
        from configs.llm_resilience import transient

        cfg = CLOUD_PARSING_RETRY_CONFIG
        for attempt in range(cfg.max_retries + 1):
            try:
                return await run_with_manager()
            except Exception as e:  # noqa: BLE001
                # openai.InternalServerError (status_code 504) или переобёрнутый OpenAIError с HTML страницы 504
                llm_overload = "openai" in type(e).__module__ and (
                    transient(e) or any(k in str(e) for k in ("504", "502", "503", "Gateway", "Timeout", "timed out")))
                if llm_overload or attempt == cfg.max_retries:
                    raise
                delay = cfg.delay * (cfg.backoff ** attempt)
                logger.warning(f"evaluate_documents_task: попытка {attempt + 1} не удалась ({type(e).__name__}), "
                               f"повтор через {delay:.0f} с")
                await asyncio.sleep(delay)

    return run_async(run_with_retry())


@celery_app.task(bind=True, name="index_documents_task", **LONG_TASK_LIMITS)
def index_documents_task(self, expertise_id: int, environment: str = None):
    """Фоновая индексация документов экспертизы и сразу за ней построение фактов.

    Запускается из :func:`knowledge_store.hooks.on_run_finished` после завершения
    ``/evaluate-documents``. Факты (при ``PE_FACTS_ENABLED=true``) строятся сразу после индексации,
    так как следом обязательно запрашивается экспертное заключение. При выключенном
    ``KNOWLEDGE_STORE_ENABLED`` ничего не делает.

    Args:
        expertise_id: ID экспертизы.
        environment: Окружение (``X-API-Database``). Сообщения, поставленные до появления параметра, относятся
            к ``PE_LEGACY_ENV`` (``configs/settings.py``).

    Returns:
        dict: ``{"index": ..., "facts": ...}`` либо ``{"skipped": True}``.
    """
    import os

    from knowledge_store.db import task_pool
    from knowledge_store.runner import facts_enabled, index_and_build_facts
    from knowledge_store.safe import is_enabled
    from knowledge_store.scope import Scope

    if not is_enabled():
        return {"skipped": True}

    async def run():
        """Создаёт HTTP-менеджер и LLM-клиент внутри event loop задачи и запускает индексацию и факты."""
        client, model = (None, None)
        if facts_enabled():
            from configs.llm_client import get_llm
            client, model = get_llm()
        from configs import settings
        scope = Scope.of(environment or settings.PE_LEGACY_ENV, expertise_id)
        async with HTTPClientManager(timeout=120.0) as mgr, task_pool():
            return await index_and_build_facts(scope, mgr, client, model)

    return run_async(run())


@celery_app.task(name="purge_expired_texts_task", **LONG_TASK_LIMITS)
def purge_expired_texts_task():
    """Ночная очистка текстов и чанков с истёкшим сроком хранения (``PE_TEXT_RETENTION_DAYS``) и кэша эмбеддингов
    подбора экспертов (векторы, не использованные 90 дней).

    Returns:
        dict: ``{"embeddings_purged": N, "purged": N}`` либо ``{"embeddings_purged": N, "skipped": True}``, если
        хранилище выключено.
    """
    from knowledge_store.indexer import purge_expired_texts
    from knowledge_store.safe import is_enabled

    result = {"embeddings_purged": run_async(_purge_text_embeddings())}
    if not is_enabled():
        return {**result, "skipped": True}
    return {**result, "purged": run_async(purge_expired_texts())}


async def _purge_text_embeddings():
    """Удаляет из кэша эмбеддингов подбора экспертов векторы, не использованные 90 дней (миграция 013).

    Returns:
        int | None: Сколько удалено; ``None`` — внутренняя БД недоступна (очистка текстов продолжается).
    """
    from knowledge_store.db import connection
    from search_experts.embedding_cache import purge_unused

    try:
        async with connection() as conn:
            return await purge_unused(conn, 90)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Кэш эмбеддингов не очищен: {type(e).__name__}: {e}")
        return None


@celery_app.task(bind=True, name="generate_summary_opinion_task", **LONG_TASK_LIMITS)
def generate_summary_opinion_task(self, expertise_id: int, environment: str, send_draft: bool = False,
                                  rebuild_facts: bool = False):
    """Генерирует сводное ЭЗ из хранилища знаний (факты → ``data`` по ключам формы, ``trace`` отдельно).

    Если фактов ещё нет и они включены (``PE_FACTS_ENABLED``), строит их по сохранённым текстам.
    При ``send_draft=True`` отправляет ``data`` в основную БД через ``ExternalAPIService`` (``Config.get_external_api_config``).

    Args:
        expertise_id: ID экспертизы.
        environment: Окружение (``X-API-Database``).
        send_draft: Отправлять ли черновик в основную БД.
        rebuild_facts: Пересчитать факты по сохранённым текстам перед сборкой.

    Returns:
        dict: ``summary_id``, ``form_code``, ``status``, ``data``, ``trace``, ``stats``, ``problems``,
        а также ``draft_sent`` (если запрошена отправка) либо ``{"error", "http_status"}``, если сборка невозможна.
    """
    from configs.llm_client import get_llm
    from knowledge_store import db, export, facts as facts_mod, indexer, runner, summary
    from knowledge_store.safe import is_enabled
    from knowledge_store.scope import Scope

    if not is_enabled():
        return {"error": "Хранилище знаний выключено (KNOWLEDGE_STORE_ENABLED=false)", "http_status": 503}

    async def run():
        """Собирает заключение внутри event loop задачи."""
        client, model = get_llm()
        llm_call = facts_mod.make_llm_call(client, model, max_tokens=2000)   # блоки III–IV: короткий структурированный JSON
        scope = Scope.of(environment, expertise_id)
        async with HTTPClientManager(timeout=120.0) as mgr, db.task_pool():
            async def build_facts():
                """Строит факты, если их ещё нет (после индексации они обычно уже есть)."""
                if runner.facts_enabled():
                    await runner.build_facts(scope, indexer.build_embedder(mgr), client, model)

            try:
                async with db.connection() as conn:
                    result = await summary.generate_summary(conn, scope, llm_call, build_facts, rebuild_facts)
            except summary.SummaryError as e:
                return {"error": e.message, "http_status": e.http_status}
            if send_draft:
                result["draft_sent"] = await export.send_draft(mgr, int(expertise_id), result["data"], environment)
            return result

    return run_async(run())


@celery_app.task(bind=True, name="expert_publications_task", **LONG_TASK_LIMITS)
def expert_publications_task(self, environment: str = None, expert_ids: list = None, sync: bool = True,
                             process: bool = True):
    """Публикации и монографии экспертов: синхронизация с внешней БД и разбор очереди.

    Запускается Celery beat раз в сутки в ``settings.EXPERT_PUB_SYNC_HOUR``:``EXPERT_PUB_SYNC_MINUTE`` (полная синхронизация окружения
    установки) и ``POST /expert-publications/sync`` (после изменения анкеты — только указанные эксперты).
    Если в очереди остались источники, ставит продолжение (только обработка, без повторной синхронизации).

    Args:
        environment: Окружение (``X-API-Database``); по умолчанию — окружение установки (``APP_ENV``).
        expert_ids: Только эти эксперты; ``None`` — все.
        sync: Сравнить снимок внешней БД с внутренней.
        process: Разобрать пачку очереди.

    Returns:
        dict: Счётчики синхронизации и обработки, сводка по окружению (``search_experts.publications.runner.run``).
    """
    from configs import settings
    from search_experts.publications.runner import run

    if not settings.EXPERT_PUB_SYNC_ENABLED:
        return {"skipped": True}
    env = environment or settings.ENVIRONMENT
    result = run_async(run(env, expert_ids=expert_ids, sync=sync, process=process))
    if process and result.get("more"):
        expert_publications_task.apply_async(kwargs={"environment": env, "sync": False}, countdown=30)
    return result


@celery_app.task(name="org_aliases_task", **LONG_TASK_LIMITS)
def org_aliases_task(environment: str = None):
    """Пополняет псевдонимы организаций для конфликта интересов (``search_experts.org_aliases``).

    Раз в сутки (Celery beat, ``settings.ORG_ALIASES_HOUR``:``ORG_ALIASES_MINUTE``): написание места работы или
    диплома эксперта, у которого появился ИНН, становится псевдонимом этого ИНН в ``org_name_aliases``.

    Args:
        environment: Окружение; по умолчанию — окружение установки (``APP_ENV``).

    Returns:
        dict: ``env``, ``experts``, ``aliases_added``.
    """
    from configs import settings
    from search_experts.org_aliases import run

    if not settings.CONFLICT_ORG_DIRECTORY_ENABLED:
        return {"skipped": True}
    return run_async(run(environment or settings.ENVIRONMENT))

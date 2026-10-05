import asyncio
from configs.http_client_manager import HTTPClientManager

from celery import current_task
from celery_app import celery_app

from configs.logger import get_logger
from evaluate_documents.evaluate_documents_pipeline import TasksPipeline
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG


logger = get_logger(__name__)
        

def run_async(coro):
    """Запускает корутину с правильным управлением event loop"""
    return asyncio.run(coro)


@celery_app.task(bind=True, name="evaluate_documents_task")
def evaluate_documents_task(self, expertise_id: int, environment: str):
    """
    Запускает полный конвейер оценки экспертов для заданной экспертизы.

    Последовательно выполняет все этапы: загрузку данных, предобработку, расчёт сходства,
    фильтрацию по конфликтам и вычисление рейтинга.

    Args:
        sql_file_path (str): Имя файла с SQL-запросом для получения данных об экспертах.
        expertise_id (str или int): Уникальный идентификатор экспертизы.

    Returns:
        pandas.DataFrame: Датафрейм с отфильтрованными и ранжированными экспертами,
        содержащий колонки 'expert_id' и 'rating'.
    """
    # ✅ Инициализируем менеджер внутри асинхронного контекста задачи

    # @async_retry(CLOUD_PARSING_RETRY_CONFIG)
    async def run_with_manager():
        async with HTTPClientManager(timeout=90.0) as mgr:
            tasks_piplene = TasksPipeline(mgr, expertise_id, environment)
            task_result = await tasks_piplene.run_pipeline()
            return task_result
        
    return run_async(async_retry(CLOUD_PARSING_RETRY_CONFIG)(run_with_manager)())
    # return asyncio.run(run_with_manager)


@celery_app.task(bind=True, name="index_documents_task")
def index_documents_task(self, expertise_id: int):
    """Фоновая индексация документов экспертизы и сразу за ней построение фактов.

    Запускается из :func:`knowledge_store.hooks.on_run_finished` после завершения
    ``/evaluate-documents``. Факты (при ``PE_FACTS_ENABLED=true``) строятся сразу после индексации,
    так как следом обязательно запрашивается экспертное заключение. При выключенном
    ``KNOWLEDGE_STORE_ENABLED`` ничего не делает.

    Args:
        expertise_id: ID экспертизы.

    Returns:
        dict: ``{"index": ..., "facts": ...}`` либо ``{"skipped": True}``.
    """
    from knowledge_store.runner import facts_enabled, index_and_build_facts
    from knowledge_store.safe import is_enabled

    if not is_enabled():
        return {"skipped": True}

    async def run():
        """Создаёт HTTP-менеджер и LLM-клиент внутри event loop задачи и запускает индексацию и факты."""
        client, model = (None, None)
        if facts_enabled():
            from configs.llm_client import get_llm
            client, model = get_llm()
        async with HTTPClientManager(timeout=120.0) as mgr:
            return await index_and_build_facts(int(expertise_id), mgr, client, model)

    return run_async(run())


@celery_app.task(name="purge_expired_texts_task")
def purge_expired_texts_task():
    """Ночная очистка текстов и чанков с истёкшим сроком хранения (``PE_TEXT_RETENTION_DAYS``).

    Returns:
        dict: ``{"purged": N}`` либо ``{"skipped": True}``, если хранилище выключено.
    """
    from knowledge_store.indexer import purge_expired_texts
    from knowledge_store.safe import is_enabled

    if not is_enabled():
        return {"skipped": True}
    return {"purged": run_async(purge_expired_texts())}


@celery_app.task(bind=True, name="generate_summary_opinion_task")
def generate_summary_opinion_task(self, expertise_id: int, environment: str, send_draft: bool = False):
    """Генерирует сводное ЭЗ из хранилища знаний (факты → ``data`` по ключам формы, ``trace`` отдельно).

    Если фактов ещё нет и они включены (``PE_FACTS_ENABLED``), строит их по сохранённым текстам.
    При ``send_draft=True`` отправляет ``data`` в основную БД через ``Config.get_external_api_config``.

    Args:
        expertise_id: ID экспертизы.
        environment: Окружение (``X-API-Database``).
        send_draft: Отправлять ли черновик в основную БД.

    Returns:
        dict: ``summary_id``, ``form_code``, ``status``, ``data``, ``trace``, ``stats``, ``problems``,
        а также ``draft_sent`` (если запрошена отправка) либо ``{"error", "http_status"}``, если сборка невозможна.
    """
    from configs.llm_client import get_llm
    from configs.working_with_db import get_async_db_connection
    from knowledge_store import export, facts as facts_mod, indexer, runner, summary
    from knowledge_store.safe import is_enabled

    if not is_enabled():
        return {"error": "Хранилище знаний выключено (KNOWLEDGE_STORE_ENABLED=false)", "http_status": 503}

    async def run():
        """Собирает заключение внутри event loop задачи."""
        client, model = get_llm()
        llm_call = facts_mod.make_llm_call(client, model, max_tokens=1200)
        async with HTTPClientManager(timeout=120.0) as mgr:
            async def build_facts():
                """Строит факты, если их ещё нет (после индексации они обычно уже есть)."""
                if runner.facts_enabled():
                    await runner.build_facts(int(expertise_id), indexer.build_embedder(mgr), client, model)

            try:
                async with get_async_db_connection() as conn:
                    result = await summary.generate_summary(conn, int(expertise_id), llm_call, build_facts)
            except summary.SummaryError as e:
                return {"error": e.message, "http_status": e.http_status}
            if send_draft:
                result["draft_sent"] = await export.send_draft(mgr, int(expertise_id), result["data"], environment)
            return result

    return run_async(run())

import os
import logging

from celery import Celery
from celery.schedules import crontab

from configs.config import Config
from configs.retry_utils import sync_retry, async_retry, API_RETRY_CONFIG, EXPERTS_RETRY_CONFIG


# Настройка логирования
logger = logging.getLogger(__name__)


# Создаем экземпляр Celery
celery_app = Celery(
    'celery_app',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    include=[
        'tasks',
        'main',  # Импортируем задачи из main.py
        'risk_monitoring.tasks',  # Риск-мониторинг: анализ файлов и XML (очередь risk_monitoring)
    ]
)


# Конфигурация Celery
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Europe/Moscow',
    enable_utc=True,

    task_time_limit=1800,      # 30 минут максимум
    task_soft_time_limit=1500, # 25 минут мягкий лимит
    worker_max_tasks_per_child=50,  # Перезапуск воркера после 50 задач
    worker_max_memory_per_child=2000000,  # 2GB - перезапуск при утечке
    
    # Настройки ретраев для Celery
    task_default_retry_delay=10,
    task_max_retries=3,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    
    # Роутинг задач с настройками ретраев
    task_routes={
        'main.parse_cloud_link_task': {
            'queue': 'parsing',
            'retry_policy': {
                'max_retries': 3,
                'interval_start': 10,
                'interval_step': 10,
                'interval_max': 30,
            }
        },
        'evaluate_documents_task': {
            'queue': 'evaluation',
            'retry_policy': {
                'max_retries': 2,
                'interval_start': 5,
                'interval_step': 10,
                'interval_max': 30,
            }
        },
        'index_documents_task': {'queue': 'evaluation'},
        'generate_summary_opinion_task': {'queue': 'evaluation'},
        'purge_expired_texts_task': {'queue': 'evaluation'},
        # Риск-мониторинг — отдельная очередь и отдельный воркер (не блокирует /evaluate-documents)
        'rm_analyze_files_task': {'queue': 'risk_monitoring'},
        'rm_analyze_xml_task': {'queue': 'risk_monitoring'},
        'main.get_experts_task': {
            'queue': 'scoring',
            'retry_policy': {
                'max_retries': 3,
                'interval_start': 5,
                'interval_step': 10,
                'interval_max': 30,
            }
        },
    },
    # Политика хранения текстов «РАО Эксперт»: ежедневная очистка в 03:30 по Москве
    beat_schedule={
        'purge-expired-texts': {
            'task': 'purge_expired_texts_task',
            'schedule': crontab(hour=3, minute=30),
        },
    },
)


# Глобальные настройки ретраев для всех задач
celery_app.conf.task_default_retry_delay = 10
celery_app.conf.task_max_retries = 3
celery_app.conf.task_time_limit = 1800
celery_app.conf.task_soft_time_limit = 1500
celery_app.conf.broker_connection_retry_on_startup = True

# Redis-брокер с task_acks_late=True: сообщение подтверждается только после завершения задачи. Если задача
# идёт дольше visibility_timeout (по умолчанию 1 ч), Redis считает её потерянной и снова кладёт в очередь —
# после завершения она запускается повторно (evaluate_documents_task 13.10.2026: 3844 с > 3600 с).
# Тайм-аут должен быть больше самой долгой задачи; одинаковый для API и всех воркеров.
# Тайм-аут больше самой долгой задачи (риск-мониторинг — до 3 ч). Обратная сторона: задача, оборванная
# пересборкой или падением воркера, вернётся в очередь только через этот тайм-аут — после пересборки её
# возвращают сразу скриптом scripts/celery_unacked.py (см. его docstring).
VISIBILITY_TIMEOUT = 4 * 3600
celery_app.conf.broker_transport_options = {"visibility_timeout": VISIBILITY_TIMEOUT}
celery_app.conf.result_backend_transport_options = {"visibility_timeout": VISIBILITY_TIMEOUT}
# Воркер берёт из очереди только задачу, которую выполняет: при остановке «зависает» не больше одной
# задачи на процесс (по умолчанию резервировалось 4 × concurrency).
celery_app.conf.worker_prefetch_multiplier = 1
# Результаты задач хранятся 7 дней (по умолчанию 1 день): после истечения GET /task/{id} отвечает pending,
# и внешняя система опрашивает такую задачу бесконечно.
celery_app.conf.result_expires = 7 * 24 * 3600


def already_succeeded(task) -> bool:
    """Задача с этим id уже успешно выполнена (повторная доставка того же сообщения брокером).

    Args:
        task: Привязанная задача (``bind=True``), ``self`` внутри задачи.

    Returns:
        bool: ``True`` — повтор, выполнять не нужно.
    """
    try:
        if task.request.id and task.AsyncResult(task.request.id).state == "SUCCESS":
            logger.warning(f"Задача {task.name}[{task.request.id}] уже выполнена — повторная доставка пропущена "
                           f"(redelivered={bool((task.request.delivery_info or {}).get('redelivered'))})")
            return True
    except Exception as e:  # noqa: BLE001 — нет связи с backend: выполняем как обычно
        logger.warning(f"Не удалось проверить статус задачи {task.request.id}: {e}")
    return False


if __name__ == '__main__':
    celery_app.start()
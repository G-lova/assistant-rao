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
)


# Глобальные настройки ретраев для всех задач
celery_app.conf.task_default_retry_delay = 10
celery_app.conf.task_max_retries = 3
celery_app.conf.task_time_limit = 1800
celery_app.conf.task_soft_time_limit = 1500
celery_app.conf.broker_connection_retry_on_startup = True


if __name__ == '__main__':
    celery_app.start()
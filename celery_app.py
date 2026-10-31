import os
import logging

from celery import Celery
from celery.schedules import crontab

from configs.config import Config
from configs.retry_utils import sync_retry, async_retry, API_RETRY_CONFIG, EXPERTS_RETRY_CONFIG
from configs import settings  # час страховки ежедневного прогона риск-мониторинга


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


# Журнал воркеров — в файл на примонтированном каталоге проекта (logs/), а не только в stdout контейнера: вывод
# `docker logs` пропадает при каждом пересоздании контейнера (`up --build`), и после пересборки логи прогона
# не достать (22.10.2026). Файл — по имени воркера (-n rm@%h → logs/celery_rm.log; beat → logs/celery_beat.log),
# с ротацией по размеру; имя и размеры — в configs/settings.py.
from celery.signals import after_setup_logger


def worker_log_path(argv=None) -> str:
    """Файл журнала процесса Celery по его аргументам запуска.

    Args:
        argv: Аргументы командной строки (по умолчанию ``sys.argv``).

    Returns:
        str: ``logs/celery_rm.log`` (воркер риск-мониторинга, ``-n rm@…`` или очередь ``risk_monitoring``),
        ``logs/celery_beat.log`` (beat) или ``logs/celery.log``.
    """
    import sys

    args = " ".join(argv if argv is not None else sys.argv)
    if " beat" in f" {args}":
        name = "celery_beat"
    elif "risk_monitoring" in args or "rm@" in args:
        name = "celery_rm"
    else:
        name = "celery"
    return os.path.join(settings.WORKER_LOG_DIR, f"{name}.log")


@after_setup_logger.connect
def _file_log(logger=None, format=None, **kwargs):  # noqa: A002 — имена аргументов задаёт сигнал Celery
    """Добавляет к журналу процесса Celery файл с ротацией (``WORKER_LOG_*`` в ``configs/settings.py``)."""
    from logging.handlers import RotatingFileHandler

    if not settings.WORKER_LOG_ENABLED or logger is None:
        return
    path = worker_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=settings.WORKER_LOG_MAX_MB * 1024 * 1024,
                                      backupCount=settings.WORKER_LOG_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(format or "[%(asctime)s: %(levelname)s/%(processName)s] %(message)s"))
        logger.addHandler(handler)
    except OSError as e:  # нет прав или места — журнал остаётся в stdout
        logger.warning(f"Журнал воркера в файл {path} не включён: {e}")


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
        'rm_analyze_contracts_task': {'queue': 'risk_monitoring'},
        'rm_analyze_orgs_task': {'queue': 'risk_monitoring'},
        'rm_typal_clauses_task': {'queue': 'risk_monitoring'},
        # публикации экспертов: скачивание, OCR и модель — та же очередь, что у документов экспертиз
        'expert_publications_task': {'queue': 'evaluation'},
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
        # Типовые положения о закупке (223-ФЗ) из ЕИС: новые редакции за последние дни (configs.settings)
        # Страховка ежедневного прогона риск-мониторинга: если Laravel не вызвал POST /risk-monitoring/daily,
        # прогон за последнюю дату выгрузки запускается сам (час — configs.settings.DAILY_WATCHDOG_HOUR)
        'rm-daily-watchdog': {
            'task': 'rm_daily_watchdog_task',
            'schedule': crontab(hour=settings.DAILY_WATCHDOG_HOUR, minute=0),
            'options': {'queue': 'risk_monitoring'},
        },
        # Незавершённые ежедневные прогоны (partial / failed / оборванные) доделываются в тот же день
        'rm-daily-resume': {
            'task': 'rm_daily_resume_task',
            'schedule': crontab(minute=f"*/{settings.DAILY_RESUME_EVERY_MIN}"),
            'options': {'queue': 'risk_monitoring'},
        },
        # Сводки за прошлую неделю и прошлый месяц из ежедневных (неполная пересобирается, когда дни догонятся)
        'rm-period-summary': {
            'task': 'rm_period_task',
            'schedule': crontab(hour=settings.PERIOD_SUMMARY_HOUR, minute=settings.PERIOD_SUMMARY_MINUTE),
            'options': {'queue': 'risk_monitoring'},
        },
        # Еженедельный пересчёт затухания рисков закупок без новых данных (концепция 10.10)
        'rm-decay-weekly': {
            'task': 'rm_decay_task',
            'schedule': crontab(day_of_week=settings.RISK_DECAY_RECALC_WEEKDAY, hour=settings.RISK_DECAY_RECALC_HOUR,
                                minute=17),
            'options': {'queue': 'risk_monitoring'},
        },
        'typal-clauses-daily': {
            'task': 'rm_typal_clauses_task',
            'schedule': crontab(hour=5, minute=40),
            'options': {'queue': 'risk_monitoring'},
        },
        # Публикации экспертов: снимок источников из внешней БД → новые / изменённые / удалённые, затем разбор очереди
        'expert-publications-sync': {
            'task': 'expert_publications_task',
            'schedule': settings.EXPERT_PUB_SYNC_EVERY_MIN * 60.0,
            'options': {'queue': 'evaluation'},
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
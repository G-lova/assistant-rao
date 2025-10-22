# Файл config_local.py позволяет «подмешивать» любые настройки pgAdmin поверх стандартных, не меняя основной config_distro.py. 
# Локальная конфигурация pgAdmin
import os

# Переопределяем проблемную переменную правильным образом
DEFAULT_SERVER = 'rao_postgres'

# Если вы хотите запускать через pgAdmin встроенные инструменты (pg_dump, psql и т. д.), укажите пути к ним:
DEFAULT_BINARY_PATHS = {
    'pg': '/usr/local/bin',
    'pg_dump': '/usr/local/bin',
    'psql': '/usr/local/bin'
}

# Задайте отдельные файлы логов и уровни для разных подсистем:
LOG_FILE = '/var/lib/pgadmin/pgadmin.log'
LOG_ROTATION_SIZE = 5242880       # 5 MB
LOG_ROTATION_BACKUP_COUNT = 3
# Более подробное логирование SQL-запросов
SQL_LOG_LEVEL = 'DEBUG'

# Параметры безопасности и аутентификации:
#   - Включить двухфакторную аутентификацию (TOTP)
#   - Ограничить доступ по IP через ALLOWED_HOSTS
# SESSION_COOKIE_SECURE = True
# REMEMBER_ME_EXPIRATION = 604800    # «Запомнить меня» на 7 дней
# ALLOWED_HOSTS = ['0.0.0.0', 'localhost', 'ragsys.local']

# Кастомизация интерфейса:
#   - Изменить логотип в шапке
#   - Подключить собственные CSS
# CUSTOM_LOGO = '/var/lib/pgadmin/static/custom/logo.png'
# CUSTOM_CSS_PATH = '/var/lib/pgadmin/static/custom/style.css'

# Настройка «фоновых процессов»:
# По умолчанию pgAdmin запускает фоновые таски через Celery, можно изменить их поведение:    
# BG_WORKER_COUNT = 2
# BG_TASK_TIME_LIMIT = 300          # сек
# BG_TASK_AUDIT_LOG_LEVEL = 'INFO'

# Если у вас есть корпоративный LDAP:
# LDAP_ENABLED = True
# LDAP_SERVER = 'ldap://ldap.local'
# LDAP_BASE_DN = 'OU=Users,DC=company,DC=local'
# LDAP_UID_FIELD = 'sAMAccountName'

# Отключаем проблемные проверки
CONSOLE_LOG_LEVEL = 40  # ERROR level
FILE_LOG_LEVEL = 40     # ERROR level

# Улучшаем стабильность
WTF_CSRF_TIME_LIMIT = None
SESSION_EXPIRATION_TIME = 1  # 1 день

# Безопасность
ENHANCED_COOKIE_PROTECTION = True

# Чтобы pgAdmin мог рассылать оповещения (например, о дедупации бэкапов), задайте SMTP-настройки:
# MAIL_SERVER = 'smtp.mail.local'
# MAIL_PORT = 587
# MAIL_USE_TLS = True
# MAIL_USERNAME = os.environ.get('SMTP_USER')
# MAIL_PASSWORD = os.environ.get('SMTP_PASS')
# MAIL_DEFAULT_SENDER = ('pgAdmin', 'noreply@ragsys.local')

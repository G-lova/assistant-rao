# mailru-downloader/Dockerfile
FROM python:3.11-slim

# Установка зависимостей
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем PHP-бинарники и скрипты
COPY php/ ./php/
COPY aria2c.exe .
COPY cloud_mail_downloader.php .
COPY server.py .

# Создаём папку для скачивания
RUN mkdir -p downloads

# Устанавливаем зависимости Python-сервера
RUN pip install fastapi uvicorn

# Делаем php.exe исполняемым (в Linux это не нужно, но если вы используете Windows — см. примечание ниже)
# В Linux-образе PHP не запустится! → см. ВАЖНО ниже.

CMD ["python", "server.py"]
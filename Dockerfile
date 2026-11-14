# Use an official Python runtime as a parent image
# FROM python:3.11-slim
FROM python:3.11-slim-bookworm

# Системные программы, которые вызывает код: antiword (.doc), libmagic (тип файла), LibreOffice (конвертация в PDF),
# poppler-utils (pdftoppm / pdftotext / pdfinfo), wkhtmltopdf (HTML → PDF), wget/unzip (установка ниже).
# Компиляторов (build-essential, gcc, make, *-dev) нет: их требовал textract (pocketsphinx собирался из исходников),
# остальные пакеты ставятся готовыми wheel. Если пакет без wheel не соберётся — вернуть build-essential.
# libgl1 / libglib2.0-0 нужны были только opencv (удалён из requirements).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        antiword \
        libmagic1 \
        libreoffice \
        poppler-utils \
        wget \
        unzip \
        wkhtmltopdf && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN wget https://www.rarlab.com/rar/unrar_5.2.5-0.1_amd64.deb && \
    dpkg -i unrar_5.2.5-0.1_amd64.deb && \
    rm unrar_5.2.5-0.1_amd64.deb

# Установка сертификатов Минцифры
RUN mkdir -p /tmp/mincifry && \
    \
    wget --no-check-certificate \
      -O /tmp/root.zip \
      https://gu-st.ru/content/lending/linux_russian_trusted_root_ca_pem.zip && \
    unzip -o /tmp/root.zip -d /tmp/mincifry && \
    \
    wget --no-check-certificate \
      -O /tmp/sub.zip \
      https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.zip && \
    unzip -o /tmp/sub.zip -d /tmp/mincifry && \
    \
    find /tmp/mincifry -type f \( -name "*.pem" -o -name "*.crt" \) | while read f; do \
        cp "$f" "/usr/local/share/ca-certificates/$(basename "${f%.*}").crt"; \
    done && \
    \
    update-ca-certificates && \
    rm -rf /tmp/mincifry /tmp/root.zip /tmp/sub.zip

# Set the working directory in the container
WORKDIR /app

# Сначала только requirements.txt: слой с пакетами пересобирается лишь при изменении списка пакетов, а не при каждой
# правке кода (раньше COPY . . стоял до pip install, и пакеты ставились заново на каждую сборку)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Словарь токенизатора tiktoken — при сборке, а не при первом запуске (configs/utils.py: get_encoding скачивает его
# из интернета, если в кэше нет)
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# Код приложения (что не попадает в образ — .dockerignore)
COPY . .

# Run the application with auto-reload
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "20142", "--reload"]

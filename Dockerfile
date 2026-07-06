# Use an official Python runtime as a parent image
# FROM python:3.11-slim
FROM python:3.11-slim-bookworm

# Install necessary system dependencies and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        python3-dev \
        ca-certificates \
        antiword \
        libmagic1 \
        libreoffice \
        poppler-utils \
        zlib1g-dev \
        wget \
        unzip \
        tar \
        gcc \
        make \
        dpkg-dev \
        libgl1 \
        libglib2.0-0 \
        libxml2-dev \
        libxslt-dev \
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

# Copy the current directory contents into the container
COPY . .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Run the application with auto-reload
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "20142", "--reload"]
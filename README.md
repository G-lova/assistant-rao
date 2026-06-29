- ├── src/                       # Основная логика
- │   ├── evaluator.py          # Анализ документов
- │   ├── scoring.py            # Подбор экспертов
- │   ├── ocr.py                # OCR обработка
- │   └── prompts.py            # Промпты для LLM
+ ├── conclusion/                # Формирование заключений РАО
+ ├── evaluate_documents/        # Оценка документов
+ ├── search_experts/           # Поиск и скоринг экспертов
+ ├── configs/                  # Конфигурация
+ ├── src/                      # Основные сервисы
+ ├── schemas/                  # JSON схемы для LLM
+ ├── prompts/                  # Промпты
+ └── xml/                      # XML шаблоны# AI Assistant RAO

AI-ассистент для комплексного анализа документов закупок и подбора экспертов.

## Описание проекта

**AI Assistant RAO** - это интеллектуальная система для автоматизации экспертизы закупочных документов, построенная на базе современных технологий машинного обучения и обработки естественного языка. Система обеспечивает полный цикл анализа документации: от определения типов документов до проверки комплектности, согласованности данных и подбора релевантных экспертов.

### Ключевые возможности

- 🤖 **Автоматическое определение типов документов** с использованием языковых моделей
- 📊 **Комплексная проверка комплектности** документов согласно требованиям законодательства
- 🔍 **Анализ согласованности данных** между различными документами закупки
- 📝 **Формирование структурированных заключений** по каждому документу
- 👥 **ML-подбор экспертов** на основе релевантности и отсутствия конфликтов интересов
- 🌐 **Web-интерфейс** для удобной работы с документами
- ⚡ **Асинхронная обработка** больших объемов данных

## Архитектура системы

### Компоненты системы

```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'primaryColor': '#ff6b6b',
    'primaryTextColor': '#ffffff',
    'primaryBorderColor': '#c92a2a',
    'lineColor': '#495057',
    'sectionBkgColor': '#f8f9fa',
    'altSectionBkgColor': '#e9ecef',
    'gridColor': '#dee2e6'
  }
}}%%
graph TB
    subgraph "Пользовательский слой"
        WEB[Gradio Web Interface]
        API_CLIENT[API Клиенты]
    end
    
    subgraph "Слой API"
        API[FastAPI Backend]
        AUTH[API Key Middleware]
    end
    
    subgraph "Сервисный слой"
        EVAL[Document Evaluator]
        EXPERT[Expert Scoring Pipeline]
        OCR[OCR Service]
        PARSER[Cloud Parser]
    end
    
    subgraph "Слой очередей"
        CELERY[Celery Worker]
        REDIS[Redis Broker]
        BEAT[Celery Beat]
        FLOWER[Flower Monitor]
    end
    
    subgraph "Слой данных"
        PG[(PostgreSQL)]
        EMBEDDING[Embedding API]
        LLM[LLM API]
    end
    
    subgraph "Внешние сервисы"
        CLOUD[Cloud Storage]
        EIS[ЕИС Integration]
    end
    
    WEB --> API
    API_CLIENT --> API
    API --> AUTH
    AUTH --> EVAL
    AUTH --> EXPERT
    
    EVAL --> OCR
    EVAL --> PARSER
    EVAL --> LLM
    
    EXPERT --> EMBEDDING
    
    API -.-> CELERY
    CELERY --> REDIS
    BEAT --> REDIS
    FLOWER --> REDIS
    
    EVAL --> PG
    EXPERT --> PG
    CELERY --> PG
    
    PARSER --> CLOUD
    API -.-> EIS
    
    style WEB fill:#ff6b6b,stroke:#c92a2a,color:#ffffff
    style API fill:#4dabf7,stroke:#1864ab,color:#ffffff
    style EVAL fill:#69db7c,stroke:#2b8a3e,color:#ffffff
    style EXPERT fill:#ffd43b,stroke:#fab005,color:#000000
    style PG fill:#868e96,stroke:#495057,color:#ffffff
    style REDIS fill:#ff8cc8,stroke:#d6336c,color:#ffffff
```

### Поток обработки документов

```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'primaryColor': '#228be6',
    'primaryTextColor': '#ffffff',
    'primaryBorderColor': '#1c7ed6',
    'lineColor': '#495057',
    'sectionBkgColor': '#f8f9fa',
    'altSectionBkgColor': '#e9ecef',
    'gridColor': '#dee2e6'
  }
}}%%
flowchart TD
    START([Загрузка документов]) --> UPLOAD{Тип загрузки}
    
    UPLOAD -->|Файлы| FILES[Обработка файлов]
    UPLOAD -->|Ссылки| LINKS[Парсинг ссылок]
    
    FILES --> EXTRACT[Извлечение текста OCR]
    LINKS --> DOWNLOAD[Скачивание документов]
    DOWNLOAD --> EXTRACT
    
    EXTRACT --> SPLIT[Разбиение на чанки]
    SPLIT --> DETECT[Определение типа документа]
    
    DETECT --> ANALYZE[Анализ чанков LLM]
    ANALYZE --> MERGE[Объединение результатов]
    
    MERGE --> SAVE[Сохранение в БД]
    SAVE --> FINAL[Финальная проверка комплектности]
    
    FINAL --> REPORT[Формирование отчета]
    REPORT --> END([Готовый результат])
    
    style START fill:#51cf66,stroke:#37b24d,color:#ffffff
    style END fill:#ff6b6b,stroke:#c92a2a,color:#ffffff
    style ANALYZE fill:#4dabf7,stroke:#1864ab,color:#ffffff
    style FINAL fill:#ffd43b,stroke:#fab005,color:#000000
```

## Технологический стек

### Backend
- **FastAPI** - высокопроизводительный веб-фреймворк
- **Python 3.11** - основной язык разработки
- **PostgreSQL** - основная база данных
- **Celery** - асинхронная обработка задач
- **Redis** - брокер сообщений и кэширование

### AI/ML компоненты
- **OpenAI API** - языковая модель для анализа документов
- **Qwen/Qwen2.5-14B-Instruct** - основная LLM
- **Scikit-learn** - ML алгоритмы для подбора экспертов
- **Embedding API** - векторизация текстов

### Обработка документов
- **Tesseract** - OCR для распознавания текста
- **python-docx** - работа с Word документами
- **PyPDF2** - работа с PDF
- **OpenCV** - обработка изображений
- **Playwright** - веб-скрапинг

### Frontend
- **Gradio** - веб-интерфейс для взаимодействия с системой
- **Flower** - мониторинг Celery задач

## Структура проекта

```
assistant-rao/
├── main.py                    # Основное FastAPI приложение
├── tasks.py                   # Celery-задача по оценке документов
├── celery_app.py              # Конфигурация Celery
├── docker-compose.yml         # Docker композиция
├── Dockerfile                 # Docker образ приложения
├── init.sql                   # Инициализация БД
├── requirements.txt           # Python зависимости
├── configs/                   # Конфигурационные модули
│   ├── config.py              # Основная конфигурация
│   ├── working_with_db.py     # Работа с БД
│   ├── parsing.py             # Парсинг документов
│   ├── retry_utils.py         # Механизмы повторных попыток
│   └── ...
├── src/                       # Основная логика
│   ├── evaluator.py           # Анализ документов
│   ├── scoring.py             # Подбор экспертов
│   ├── ocr.py                 # OCR обработка
│   └── prompts.py             # Промпты для LLM
├── conclusion/                # Формирование заключений РАО
├── evaluate_documents/        # Оценка документов
├── search_experts/            # Подбор и скоринг экспертов
│   ├── pipeline.py            # Основной пайплайн
│   ├── data_fetcher.py        # Загрузка данных
│   ├── embedding_client.py    # Клиент эмбеддингов
│   ├── text_processor.py      # Обработка текстов
│   └── conflict_detector.py   # Детектор конфликтов
├── schemas/                   # JSON схемы для LLM
├── prompts/                   # Промпты
└── xml/                       # XML шаблоны
└── test/                      # Тесты
    └── load_test.py           # Нагрузочные тесты
```

## API Эндпоинты

### Основные эндпоинты

#### `POST /evaluate-documents`
Комплексный анализ документов закупки

**Параметры:**
- `procurement_id` (str): ID закупки
- `files` (List[File]): Файлы документов
- `links` (List[str]): Ссылки на документы
- `legislation` (str): Тип законодательства (44-ФЗ, 223-ФЗ)
- `procurement_method` (str): Способ закупки
- `expertise_details` (str): Тип экспертизы

**Ответ:** JSON с результатами анализа

#### `POST /get-contract-info`
Получение реквизитов контракта

**Параметры:**
- `procurement_id` (str): ID закупки

**Ответ:** JSON с номером, суммой и датой контракта

#### `POST /get-experts-for-expertise`
Подбор экспертов для экспертизы

**Параметры:**
- `expertise_id` (int): ID экспертизы

**Ответ:** Список ID подходящих экспертов

#### `GET /health`
Проверка работоспособности сервиса

**Ответ:** JSON со статусом сервиса

## Развертывание

### Требования
- Docker и Docker Compose
- Python 3.11+
- PostgreSQL 15+
- Redis 7+

### Запуск через Docker Compose

1. **Клонирование репозитория:**
```bash
git clone <repository-url>
cd assistant-rao
```

2. **Настройка переменных окружения:**
```bash
cp .env.example .env
# Отредактировать .env с необходимыми параметрами
```

3. **Запуск системы:**
```bash
docker-compose up -d
```

4. **Проверка работоспособности:**
```bash
curl http://localhost:2300/health
```

### Порты сервисов
- **FastAPI API**: `2300:20142`
- **Gradio Interface**: `2301:20141`
- **PostgreSQL**: `2302:5432`
- **Redis**: `6379:6379`
- **Flower**: `5555:5555`

## Конфигурация

### Переменные окружения

#### База данных
- `DB_HOST`: хост PostgreSQL
- `DB_PORT`: порт PostgreSQL
- `DB_NAME`: имя базы данных
- `DB_USER`: пользователь БД
- `DB_PASSWORD`: пароль БД

#### AI модели
- `MODEL_API_URL`: URL API языковой модели
- `MODEL_NAME`: название модели
- `MODEL_API_KEY`: ключ доступа к API
- `EMBEDDING_URL`: URL сервиса эмбеддингов
- `EMBEDDING_MODEL`: модель эмбеддингов

#### Безопасность
- `API_KEY`: ключ для доступа к API
- `API_KEY_HASH`: хеш ключа

#### Приложение
- `APP_ENV`: окружение (development/production)
- `LOG_LEVEL`: уровень логирования
- `MODEL_TEMPERATURE`: температура модели
- `MODEL_MAX_TOKENS`: максимальное количество токенов

## Особенности архитектуры

### 1. Модульная структура
Система построена по миксервисной архитектуре с четким разделением ответственности между компонентами.

### 2. Асинхронная обработка
Использование Celery позволяет обрабатывать большие объемы документов без блокировки основного API.

### 3. Отказоустойчивость
Внедрены механизмы повторных попыток для всех внешних вызовов и операций с БД.

### 4. Масштабируемость
Горизонтальное масштабирование воркеров Celery для обработки пиковых нагрузок.

### 5. Безопасность
Многоуровневая аутентификация и валидация входных данных.

## Мониторинг

### Flower
Веб-интерфейс для мониторинга Celery задач доступен по адресу:
```
http://localhost:5555
```

### Логирование
Система использует структурированное логирование с различными уровнями детализации.

## Тестирование

### Запуск нагрузочных тестов
```bash
python test/load_test.py
```

### Покрытие тестами
- Unit тесты для основных модулей
- Интеграционные тесты API
- Нагрузочные тесты для проверки производительности

## Лицензия

[Информация о лицензии]

## Контакты

[Контактная информация]
import os

from dotenv import load_dotenv
from dataclasses import dataclass


load_dotenv()


@dataclass
class DatabaseConfig:
    """Конфигурация базы данных"""
    url: str
    api_key: str
    storage_path: str
    
    @property
    def headers(self):
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }


@dataclass
class EmbeddingConfig:
    """Конфигурация сервиса эмбеддингов"""
    api_url: str
    api_key: str
    model: str
    batch_size: int = 16


@dataclass
class MModelConfig:
    """Конфигурация мультимодальной LLM"""
    api_url: str
    api_key: str
    model: str


@dataclass
class PathConfig:
    """Конфигурация путей"""
    sql_queries: str
    outputs: str
    logs: str = "logs/"


@dataclass
class RetryConfig:
    """Конфигурация повторных попыток"""
    max_retries: int = 3
    default_delay: float = 1.0
    backoff_factor: float = 2.0


class Config:
    """
    Класс для хранения и управления конфигурационными параметрами приложения.

    Содержит настройки модели, API и безопасности, загружаемые из переменных окружения.
    Предоставляет метод для получения конфигурации генерации модели.
    """
    # Model settings
    TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", 0.7))
    MAX_NEW_TOKENS = int(os.getenv("MODEL_MAX_TOKENS", 4096))
    
    # API settings
    URL = os.getenv("API_URL")
    MODEL_API_URL = os.getenv("MODEL_API_URL")
    MODEL_NAME = os.getenv("MODEL_NAME")
    MODEL_API_KEY = os.getenv("MODEL_API_KEY")
    
    EXTERNAL_API_URL_DEV = os.getenv("EXTERNAL_API_URL_DEV")
    EXTERNAL_API_URL_STAGE = os.getenv("EXTERNAL_API_URL_STAGE")
    EXTERNAL_API_URL_PROD = os.getenv("EXTERNAL_API_URL_PROD")
    EXTERNAL_API_KEY = os.getenv("EXTERNAL_API_KEY")
    # Security
    API_KEY = os.getenv("API_KEY")
    API_KEY_HASH = os.getenv("API_KEY_HASH")

    # Model qwen-vl
    M_MODEL_API_URL = os.getenv("M_MODEL_API_URL")
    M_MODEL_NAME = os.getenv("M_MODEL_NAME")
    M_MODEL_API_KEY = os.getenv("M_MODEL_API_KEY")
    
    # MySQL
    MYSQL_URL_PROD = os.getenv("MYSQL_URL_PROD")
    MYSQL_API_KEY_PROD = os.getenv("MYSQL_API_KEY_PROD")
    MYSQL_URL_STAGE = os.getenv("MYSQL_URL_STAGE")
    MYSQL_API_KEY_STAGE = os.getenv("MYSQL_API_KEY_STAGE")
    MYSQL_URL_DEV = os.getenv("MYSQL_URL_DEV")
    MYSQL_API_KEY_DEV = os.getenv("MYSQL_API_KEY_DEV")

    # Postgres
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT")
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    
    # Qwen/Qwen3-Embedding-0.6B
    EMBEDDING_URL = os.getenv("EMBEDDING_URL")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY")
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "16"))
    
    # Paths
    SQL_QUERIES_PATH = os.getenv("SQL_QUERIES_PATH", "data/queries/")
    OUTPUT_PATH = os.getenv("OUTPUT_PATH", "data/outputs/")
    LOG_PATH = os.getenv("LOG_PATH", "logs/")
    STORAGE_PATH_DEV = os.getenv("STORAGE_PATH_DEV", "https://develop.rao0123.1t.ws/storage/")
    STORAGE_PATH_STAGE = os.getenv("STORAGE_PATH_STAGE", "https://stage.rao0123.1t.ws/storage/")
    STORAGE_PATH_PROD = os.getenv("STORAGE_PATH_PROD", "https://expert.rusacademedu.ru/storage/")
    
    # Application
    APP_ENV = os.getenv("APP_ENV", "development")
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "logs/app.log")

    # Retry settings
    RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
    RETRY_DELAY = float(os.getenv("RETRY_DELAY", "1.0"))
    RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "2.0"))

    @classmethod
    def get_retry_config(cls) -> RetryConfig:
        """
        Возвращает конфигурацию повторных попыток.
        """
        return RetryConfig(
            max_retries=cls.RETRY_MAX_ATTEMPTS,
            default_delay=cls.RETRY_DELAY,
            backoff_factor=cls.RETRY_BACKOFF
        )

    @classmethod
    def get_model_config(cls):
        return {
            "temperature": cls.TEMPERATURE,
            "max_new_tokens": cls.MAX_NEW_TOKENS
        }
    

    @classmethod
    def get_m_model_config(cls) -> MModelConfig:
        """
        Возвращает конфигурацию сервиса эмбеддингов.
        """
        return MModelConfig(
            api_url=cls.M_MODEL_API_URL,
            api_key=cls.M_MODEL_API_KEY,
            model = cls.M_MODEL_NAME
        )
    
    @classmethod
    def get_database_config(cls, environment: str = None) -> DatabaseConfig:
        """
        Возвращает конфигурацию базы данных для scoring pipeline.
        
        Args:
            environment: Окружение ('dev', 'stage', 'prod'). 
                        Если None, используется APP_ENV
        """
        env = environment or cls.APP_ENV
        env = env.lower()
        
        if env in ['prod', 'production']:
            url = cls.MYSQL_URL_PROD
            api_key = cls.MYSQL_API_KEY_PROD
            storage_path = cls.STORAGE_PATH_PROD
        elif env in ['stage', 'staging']:
            url = cls.MYSQL_URL_STAGE
            api_key = cls.MYSQL_API_KEY_STAGE
            storage_path = cls.STORAGE_PATH_STAGE
        elif env in ['dev', 'development']:
            url = cls.MYSQL_URL_DEV
            api_key = cls.MYSQL_API_KEY_DEV
            storage_path = cls.STORAGE_PATH_DEV
        else:
            url = cls.MYSQL_URL_DEV
            api_key = cls.MYSQL_API_KEY_DEV
            storage_path = cls.STORAGE_PATH_DEV
        
        return DatabaseConfig(url=url, api_key=api_key, storage_path=storage_path)
    
    @classmethod
    def get_embedding_config(cls) -> EmbeddingConfig:
        """
        Возвращает конфигурацию сервиса эмбеддингов.
        """
        return EmbeddingConfig(
            api_url=cls.EMBEDDING_URL,
            api_key=cls.EMBEDDING_API_KEY,
            model = cls.EMBEDDING_MODEL,
            batch_size=cls.BATCH_SIZE
        )
    
    @classmethod
    def get_paths_config(cls) -> PathConfig:
        """
        Возвращает конфигурацию путей.
        """
        return PathConfig(
            sql_queries=cls.SQL_QUERIES_PATH,
            outputs=cls.OUTPUT_PATH,
            logs=cls.LOG_PATH
        )
    
    @classmethod
    def get_scoring_config(cls):
        """
        Возвращает полную конфигурацию для scoring pipeline.
        """
        return {
            "environment": cls.APP_ENV,
            "debug": cls.DEBUG,
            "log_level": cls.LOG_LEVEL
        }
    @classmethod
    def get_external_api_config(cls, environment: str = None) -> dict:
        """
        Возвращает конфигурацию для внешнего API в зависимости от среды
        
        Args:
            environment: Окружение ('dev', 'stage', 'prod'). Если None, используется APP_ENV
            
        Returns:
            dict: Конфигурация с url и headers
        """
        env = environment or cls.APP_ENV
        env = env.lower()
        
        # Выбираем URL в зависимости от среды
        if env in ['prod', 'production']:
            url = cls.EXTERNAL_API_URL_PROD
        elif env in ['stage', 'staging']:
            url = cls.EXTERNAL_API_URL_STAGE
        else:  # dev, development или любое другое
            url = cls.EXTERNAL_API_URL_DEV
        
        
        return {
            "url": url.rstrip('/'),  # Убираем лишний слеш
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-API-Key": cls.EXTERNAL_API_KEY
            }
        }
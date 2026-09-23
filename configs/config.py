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
    media_path: str
    
    @property
    def headers(self):
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }


@dataclass
class UpdateExpertiseDBConfig:
    """Конфигурация сервиса обновления экспертизы в базе данных"""
    url: str
    api_key: str
    
    @property
    def headers(self):
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }


@dataclass
class UpdateAIDBConfig:
    """Конфигурация сервиса обновления экспертизы в базе данных"""
    url: str
    bearer_key: str
    
    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.bearer_key}",
            "Accept": "application/json",
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
    # Security
    API_KEY = os.getenv("API_KEY")
    API_KEY_HASH = os.getenv("API_KEY_HASH")

    # SYSTEM API
    SYSTEM_URL_DEV = os.getenv("SYSTEM_URL_DEV")
    SYSTEM_URL_STAGE = os.getenv("SYSTEM_URL_STAGE")
    SYSTEM_URL_PROD = os.getenv("SYSTEM_URL_PROD")
    SYSTEM_URL_NEURO = os.getenv("SYSTEM_URL_NEURO")
    SYSTEM_API_KEY = os.getenv("SYSTEM_API_KEY")
    SYSTEM_BEARER_KEY = os.getenv("SYSTEM_BEARER_KEY")
    
    # LLM API settings
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    MODEL_API_URL = os.getenv("MODEL_API_URL")
    MODEL_NAME = os.getenv("MODEL_NAME")
    
    TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", 0.7))
    MAX_NEW_TOKENS = int(os.getenv("MODEL_MAX_TOKENS", 4096))

    # Model qwen-vl
    M_MODEL_API_URL = os.getenv("M_MODEL_API_URL")
    M_MODEL_NAME = os.getenv("M_MODEL_NAME")
    
    # Qwen/Qwen3-Embedding-0.6B
    EMBEDDING_URL = os.getenv("EMBEDDING_URL")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "16"))

    # Postgres
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT")
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    
    # Paths
    SQL_QUERIES_PATH = os.getenv("SQL_QUERIES_PATH", "queries/")
    OUTPUT_PATH = os.getenv("OUTPUT_PATH", "data/outputs/")
    LOG_PATH = os.getenv("LOG_PATH", "logs/")
    
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
            api_key=cls.OPENAI_API_KEY,
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
        api_key = cls.SYSTEM_API_KEY
        
        if env in ['prod', 'production']:
            url = f"{cls.SYSTEM_URL_PROD}/api/database/query"
        elif env in ['stage', 'staging']:
            url = f"{cls.SYSTEM_URL_STAGE}/api/database/query"
        elif env in ['neuro_assistant_database', 'neuro']:
            url = f"{cls.SYSTEM_URL_NEURO}/api/database/query"
        else:
            url = f"{cls.SYSTEM_URL_DEV}/api/database/query"

        storage_path = f"{url.replace('/api/database/query', '')}/storage/"
        media_path = f"{url.replace('/database/query', '')}/media/"
        
        return DatabaseConfig(url=url, api_key=api_key, storage_path=storage_path, media_path=media_path)
    
    @classmethod
    def get_embedding_config(cls) -> EmbeddingConfig:
        """
        Возвращает конфигурацию сервиса эмбеддингов.
        """
        return EmbeddingConfig(
            api_url=cls.EMBEDDING_URL,
            api_key=cls.OPENAI_API_KEY,
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
            url = f"{cls.SYSTEM_URL_PROD}/api/expertise/set-hint-for-rao-expert"
        elif env in ['stage', 'staging']:
            url = f"{cls.SYSTEM_URL_STAGE}/api/expertise/set-hint-for-rao-expert"
        elif env in ['neuro_assistant_database', 'neuro']:
            url = f"{cls.SYSTEM_URL_NEURO}/api/expertise/set-hint-for-rao-expert"
        else:  # dev, development или любое другое
            url = f"{cls.SYSTEM_URL_DEV}/api/expertise/set-hint-for-rao-expert"
        
        
        return {
            "url": url.rstrip('/'),  # Убираем лишний слеш
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-API-Key": cls.SYSTEM_API_KEY
            }
        }
    
    @classmethod
    def get_update_db_config(cls, environment: str = None) -> UpdateExpertiseDBConfig:
        """
        Возвращает конфигурацию базы данных для scoring pipeline.
        
        Args:
            environment: Окружение ('dev', 'stage', 'prod'). 
                        Если None, используется APP_ENV
        """
        env = environment or cls.APP_ENV
        env = env.lower()
        api_key = cls.SYSTEM_API_KEY
        
        if env in ['prod', 'production']:
            url = f"{cls.SYSTEM_URL_PROD}/api/data/update"
        elif env in ['stage', 'staging']:
            url = f"{cls.SYSTEM_URL_STAGE}/api/data/update"
        elif env in ['neuro_assistant_database', 'neuro']:
            url = f"{cls.SYSTEM_URL_NEURO}/api/data/update"
        else:
            url = f"{cls.SYSTEM_URL_DEV}/api/data/update"
        
        return UpdateExpertiseDBConfig(url=url, api_key=api_key)
    
    @classmethod
    def get_update_ai_db_config(cls, environment: str = None) -> UpdateAIDBConfig:
        """
        Возвращает конфигурацию базы данных для scoring pipeline.
        
        Args:
            environment: Окружение ('dev', 'stage', 'prod'). 
                        Если None, используется APP_ENV
        """
        env = environment or cls.APP_ENV
        env = env.lower()
        bearer_key = cls.SYSTEM_BEARER_KEY
        
        if env in ['prod', 'production']:
            url = f"{cls.SYSTEM_URL_PROD}/api/risk-monitoring/ai-analysis"
        elif env in ['stage', 'staging']:
            url = f"{cls.SYSTEM_URL_STAGE}/api/risk-monitoring/ai-analysis"
        elif env in ['neuro_assistant_database', 'neuro']:
            url = f"{cls.SYSTEM_URL_NEURO}/api/risk-monitoring/ai-analysis"
        else:
            url = f"{cls.SYSTEM_URL_DEV}/api/risk-monitoring/ai-analysis"
        
        return UpdateAIDBConfig(url=url, bearer_key=bearer_key)
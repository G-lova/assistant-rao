import os

from dotenv import load_dotenv


load_dotenv()


class Config:
    """
    Конфигурационный класс для хранения настроек подключения к Qdrant и модели эмбеддингов.

    Содержит параметры, загружемые из переменных окружения, включая URL и ключи доступа
    к векторной базе данных Qdrant, а также настройки модели генерации эмбеддингов.
    Используется для централизованного управления конфигурацией поиска и хранения документов.
    """
    QDRANT_URL = os.getenv("QDRANT_URL")
    QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
    EMBEDDING_MODEL_URL = os.getenv("EMBEDDING_MODEL_URL")
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY")
    EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", 1024))
    COLLECTION_NAME = os.getenv("COLLECTION_NAME", "documents")
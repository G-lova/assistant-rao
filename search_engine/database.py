import os

from qdrant_client import QdrantClient
from qdrant_client.http import models
from dotenv import load_dotenv


load_dotenv()


class QdrantDatabase:
    """
    Обёртка для работы с векторной базой данных Qdrant.

    Предоставляет методы для создания коллекции, загрузки документов (векторов и метаданных)
    и поиска похожих документов по векторному представлению. Используется для хранения
    и семантического поиска юридических и закупочных документов.
    """

    def __init__(self):
        """
        Инициализирует клиент Qdrant с параметрами из переменных окружения.
        """
        self.client = QdrantClient(
            host=os.getenv("QDRANT_HOST"),
            port=int(os.getenv("QDRANT_PORT")),
            api_key=os.getenv("QDRANT_API_KEY"),
            https=False,
        )
        self.collection_name = os.getenv("COLLECTION_NAME")


    def create_collection(self, vector_size):
        """
        Создаёт или пересоздаёт коллекцию в Qdrant с заданным размером векторов.

        Args:
            vector_size: Размерность векторов эмбеддингов, которые будут храниться в коллекции.
        """
        self.client.recreate_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE
            )
        )


    def upload_document(self, document_id, vector, payload):
        """
        Загружает документ (вектор + метаданные) в коллекцию Qdrant.

        Args:
            document_id: Уникальный идентификатор документа.
            vector: Векторное представление документа (список чисел).
            payload: Дополнительные данные о документе (например, имя, тип, контекст).
        """
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=document_id,
                    vector=vector,
                    payload=payload
                )
            ]
        )


    def search_similar(self, vector, limit=3):
        """
        Выполняет поиск похожих документов по заданному вектору.

        Args:
            vector: Запрос-вектор для поиска (список чисел).
            limit (int, optional): Максимальное количество возвращаемых результатов. По умолчанию 3.

        Returns:
            Список похожих документов с их метаданными и расстоянием (схожестью).
        """
        return self.client.search(
            collection_name=self.collection_name,
            query_vector=vector,
            limit=limit,
            with_payload=True
        )
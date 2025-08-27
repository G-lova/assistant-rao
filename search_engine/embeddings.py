import os

import requests
from typing import List


class QwenEmbedder:
    """
    Класс для генерации эмбеддингов с использованием модели Qwen через API.

    Предоставляет интерфейс для получения векторных представлений текстов
    с помощью удалённой модели эмбеддингов (например, Qwen3-Embedding).
    Поддерживает батчевую обработку нескольких текстов.
    """

    def __init__(self):
        """
        Инициализирует embedder, устанавливая URL и ключ API из переменных окружения.
        """
        self.api_url = os.getenv("EMBEDDING_MODEL_URL") + "/v1/embeddings"
        self.api_key = os.getenv("EMBEDDING_API_KEY", "EMPTY")


    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        Генерирует эмбеддинги для списка текстов.

        Отправляет запрос к API модели эмбеддингов и возвращает векторные представления.

        Args:
            texts (List[str]): Список текстов, для которых нужно получить эмбеддинги.

        Raises:
            RuntimeError: Если запрос к API не удался или вернул ошибку.

        Returns:
            List[List[float]]: Список векторов-эмбеддингов, каждый — список чисел (размерность задаётся моделью).
        """
        if isinstance(texts, str):
            texts = [texts]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": os.getenv("EMBEDDING_MODEL_NAME", "Qwen/Qwen3-Embedding-0.6B"),
            "input": texts
        }

        try:
            response = requests.post(self.api_url, json=data, headers=headers)
            response.raise_for_status()
            result = response.json()
            # Извлекаем эмбеддинги
            embeddings = [item["embedding"] for item in result["data"]]
            return embeddings
        except Exception as e:
            raise RuntimeError(f"Ошибка при получении эмбеддингов: {str(e)}")


    def get_embedding_size(self) -> int:
        """
        Возвращает размерность эмбеддингов, указанную в конфигурации.

        Returns:
            int: Размерность вектора эмбеддинга (1024).
        """
        return int(os.getenv("EMBEDDING_DIM", 1024))
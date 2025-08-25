# search_engine/embeddings.py
import requests
import os
from typing import List

class QwenEmbedder:
    def __init__(self):
        self.api_url = os.getenv("EMBEDDING_MODEL_URL") + "/v1/embeddings"
        self.api_key = os.getenv("EMBEDDING_API_KEY", "EMPTY")

    def embed(self, texts: List[str]) -> List[List[float]]:
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
        return int(os.getenv("EMBEDDING_DIM", 1024))
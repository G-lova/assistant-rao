# search_experts/embedding_client.py
import asyncio
import json
import logging
import numpy as np
from typing import List, Optional
from aiohttp import ClientSession, ClientTimeout

# Импортируем наш менеджер
from configs.http_client_manager import HTTPClientManager

logger = logging.getLogger(__name__)

class EmbeddingClient:
    """
    Клиент для получения векторных эмбеддингов текста из внешнего API.
    Использует общий HTTPClientManager для управления соединениями.
    """
    
    def __init__(
        self, 
        api_url: str, 
        api_key: str, 
        model: str, 
        batch_size: int,
        http_manager: HTTPClientManager 
    ):
        """
        Args:
            api_url: URL эндпоинта API эмбеддингов
            api_key: Ключ аутентификации
            model: Название модели
            batch_size: Размер пакета для пакетной обработки
            http_manager: Глобальный менеджер HTTP-сессий (обязателен)
        """
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch_size = batch_size
        self.http_manager = http_manager 
        
        # Ограничитель параллелизма (остаётся локальным, т.к. относится к бизнес-логике)
        self._semaphore = asyncio.Semaphore(10)
        
        logger.info(f"EmbeddingClient initialized with shared HTTP manager: {api_url}")

    async def get_embeddings(self, texts: List[str]) -> np.ndarray:
        """
        Асинхронно генерирует эмбеддинги для списка текстов.
        """
        if not texts:
            return np.array([])
        
        tasks = []
        # Разбиваем на пакеты
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            task = self._get_embeddings_batch(batch)
            tasks.append(task)
        
        # Выполняем все пакеты параллельно
        all_batches = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Собираем результаты, фильтруя ошибки
        all_embeddings = []
        for batch_result in all_batches:
            if isinstance(batch_result, Exception):
                logger.error(f"Ошибка в пакете эмбеддингов: {batch_result}")
                continue
            if batch_result:
                all_embeddings.extend(batch_result)
        
        return np.array(all_embeddings) if all_embeddings else np.array([])

    async def _get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Получает эмбеддинги для одного пакета текстов через общий aiohttp-сессия.
        """
        # Формируем payload (совместимый с OpenAI-like API)
        input_data = texts[0] if len(texts) == 1 else texts
        payload = {
            "model": self.model,
            "input": input_data
        }
        
        # Формируем заголовки
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key

        # ✅ Получаем сессию из менеджера
        session: ClientSession = self.http_manager.get_session()
        
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # ✅ Ограничиваем параллелизм на уровне бизнес-логики
                async with self._semaphore:
                    async with session.post(
                        self.api_url,
                        json=payload,
                        headers=headers,
                        timeout=ClientTimeout(total=30, connect=5)
                    ) as response:
                        
                        # Обработка ошибок
                        if response.status == 400 and len(texts) > 1:
                            # Рекурсивное разбиение пакета при 400 ошибке
                            logger.warning(f"400 ошибка, разбиваем пакет: {len(texts)} текстов")
                            mid = len(texts) // 2
                            first = await self._get_embeddings_batch(texts[:mid])
                            second = await self._get_embeddings_batch(texts[mid:])
                            return first + second
                        
                        response.raise_for_status()
                        data = await response.json()
                        
                        # Парсинг ответа (поддержка разных форматов)
                        embeddings = self._extract_embeddings(data)
                        if embeddings is not None:
                            return embeddings
                        
                        logger.error(f"Неизвестный формат ответа: {data}")
                        raise ValueError("No embeddings in response")
                        
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # экспоненциальная задержка
                    logger.warning(f"Попытка {attempt + 1}/{max_retries} не удалась: {e}. Ждём {wait_time}с...")
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(f"Исчерпаны попытки после {max_retries} попыток: {e}")
                raise last_error
        
        raise last_error

    @staticmethod
    def _extract_embeddings(data: dict) -> Optional[List[List[float]]]:
        """
        Извлекает эмбеддинги из ответа API, поддерживая разные форматы.
        """
        # Формат 1: одиночный эмбеддинг
        if "embedding" in data and isinstance(data["embedding"], list):
            return [data["embedding"]]
        
        # Формат 2: множественные эмбеддинги
        if "embeddings" in data and isinstance(data["embeddings"], list):
            return data["embeddings"]
        
        # Формат 3: OpenAI-style с массивом data
        if "data" in data and isinstance(data["data"], list):
            return [item["embedding"] for item in data["data"] if "embedding" in item]
        
        return None
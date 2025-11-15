import numpy as np
import httpx
import logging

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """
    Клиент для получения векторных эмбеддингов текста из внешнего API.

    Класс позволяет преобразовать список текстов в их числовые векторные представления (эмбеддинги)
    с помощью удалённой модели. Поддерживает пакетную обработку для повышения эффективности.
    """
    
    def __init__(self, api_url, api_key, model, batch_size):
        """
        Инициализирует клиент для работы с API генерации эмбеддингов.

        Args:
            api_url (str): URL эндпоинта API, принимающего запросы на генерацию эмбеддингов.
            api_key (str): Ключ аутентификации для доступа к API.
            batch_size (int): Максимальное количество текстов, отправляемых за один запрос.
        """
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.batch_size = batch_size
        
        # Используем URL из настроек без изменений
        logger.info(f"Initialized EmbeddingClient with URL: {self.api_url}")
    
    
    def get_embeddings(self, texts):
        """
        Генерирует векторные эмбеддинги для списка текстов.

        Разбивает входной список текстов на пакеты заданного размера, отправляет их в API
        и собирает полученные векторные представления. Возвращает массив numpy со всеми эмбеддингами.

        Args:
            texts (List[str]): Список текстовых строк, для которых необходимо получить эмбеддинги.

        Returns:
            np.ndarray: Двумерный массив формы (len(texts), embedding_dim), где каждая строка —
                        векторное представление соответствующего текста.
                        
        Raises:
            httpx.HTTPError: Если запрос к API завершился неуспешно (например, 4xx или 5xx).
            KeyError: Если в ответе API отсутствует ожидаемое поле 'embeddings'.
        """
        all_embeddings = []
        
        # Разбиваем на пакеты в соответствии с batch_size
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            batch_embs = self._get_embeddings_direct(batch)
            all_embeddings.extend(batch_embs)

        return np.array(all_embeddings)
    
    def _get_embeddings_direct(self, texts):
        """
        Получает эмбеддинги напрямую через HTTP запрос к Ollama API.
        
        Args:
            texts (List[str]): Список текстов для эмбеддинга
            
        Returns:
            List[List[float]]: Список векторов эмбеддингов
        """
        headers = {
            "X-API-Key": f"{self.api_key}",
            "Content-Type": "application/json"
        }
        
        # Для Ollama API используем правильный формат payload
        # Если один текст, отправляем как строку, если несколько - как список
        if len(texts) == 1:
            input_data = texts[0]
        else:
            input_data = texts
        
        payload = {
            # "model": self.model,
            "inputs": input_data
        }
        
        logger.info(f"Sending request to {self.api_url}")
        logger.info(f"Model: {self.model}")
        logger.info(f"Texts count: {len(texts)}")
        logger.info(f"Request headers: {headers}")
        logger.info(f"Request payload keys: {list(payload.keys())}")
        
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self.api_url,
                    json=payload,
                    headers=headers
                )
                
                logger.info(f"Response status: {response.status_code}")
                logger.info(f"Response headers: {dict(response.headers)}")
                
                if response.status_code != 200:
                    logger.error(f"Error response: {response.text}")
                    response.raise_for_status()
                
                response_data = response.json()
                logger.info(f"Response data keys: {list(response_data.keys())}")
                
                # Проверяем структуру ответа от Ollama API
                if "embedding" in response_data:
                    # Одиночный эмбеддинг
                    logger.info(f"Found single embedding in response")
                    return [response_data["embedding"]]
                elif "embeddings" in response_data:
                    # Множественные эмбеддинги
                    logger.info(f"Found embeddings in response, count: {len(response_data['embeddings'])}")
                    return response_data["embeddings"]
                elif "data" in response_data:
                    # Формат ответа как у OpenAI
                    logger.info(f"Found data in response, count: {len(response_data['data'])}")
                    return [item["embedding"] for item in response_data["data"]]
                else:
                    logger.error(f"Unexpected response format: {response_data}")
                    raise KeyError("No embeddings found in response")
                    
        except httpx.HTTPError as e:
            logger.error(f"HTTP error occurred: {e}")
            raise
        except Exception as e:
            logger.error(f"Error getting embeddings: {e}")
            raise

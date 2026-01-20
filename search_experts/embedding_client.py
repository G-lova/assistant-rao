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
            "Content-Type": "application/json"
        }
        
        # Добавляем API ключ только если он не пустой
        if self.api_key:
            # Используем стандартный формат Authorization Bearer для совместимости с OpenAI API
            headers["Authorization"] = f"Bearer {self.api_key}"
            # Также оставляем X-API-Key как запасной вариант для обратной совместимости
            headers["X-API-Key"] = f"{self.api_key}"
        
        # Для Ollama API используем правильный формат payload
        # Если один текст, отправляем как строку, если несколько - как список
        if len(texts) == 1:
            input_data = texts[0]
        else:
            input_data = texts
        
        # Пробуем разные форматы payload для совместимости
        # Формат 1: стандартный OpenAI
        payload = {
            "model": self.model,
            "input": input_data
        }
        
        # Если предыдущий формат не сработает, пробуем альтернативный
        # payload = {
        #     "model": self.model,
        #     "prompt": input_data if len(texts) == 1 else input_data
        # }
        
        logger.info(f"Sending request to {self.api_url}")
        logger.info(f"Model: {self.model}")
        logger.info(f"Texts count: {len(texts)}")
        # Маскируем API ключ в логах для безопасности
        masked_headers = headers.copy()
        if "Authorization" in masked_headers:
            auth_parts = masked_headers["Authorization"].split(" ")
            if len(auth_parts) > 1:
                masked_headers["Authorization"] = f"{auth_parts[0]} {auth_parts[1][:4]}{'*' * (len(auth_parts[1]) - 4)}"
        if "X-API-Key" in masked_headers:
            masked_headers["X-API-Key"] = f"{masked_headers['X-API-Key'][:4]}{'*' * (len(masked_headers['X-API-Key']) - 4)}"
        logger.info(f"Request headers (masked): {masked_headers}")
        logger.info(f"Request payload keys: {list(payload.keys())}")
        
        # Добавляем логирование размера данных для отладки
        total_chars = sum(len(text) for text in texts)
        logger.info(f"Total characters in texts: {total_chars}")
        logger.info(f"Average characters per text: {total_chars / len(texts) if texts else 0}")
        
        # Логируем первые 100 символов каждого текста для отладки
        for i, text in enumerate(texts[:3]):  # Логируем только первые 3 текста
            logger.info(f"Text {i+1} preview: {text[:100]}...")
            logger.info(f"Text {i+1} length: {len(text)} characters")
            logger.info(f"Text {i+1} repr: {repr(text[:200])}")
        
        # Добавляем логирование полного payload для отладки (только для первого текста)
        if len(texts) == 1:
            import json
            logger.info(f"Full request payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")
            # Проверяем, есть ли непечатаемые символы
            text = texts[0]
            try:
                text.encode('utf-8')
                logger.info("Text encoding: OK")
            except UnicodeEncodeError as e:
                logger.error(f"Text encoding error: {e}")
        
        # Максимальное количество повторных попыток
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
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
                        
                        # Если это 400 ошибка, попробуем уменьшить размер пакета
                        if response.status_code == 400 and len(texts) > 1:
                            logger.warning(f"Received 400 error, retrying with smaller batch size")
                            # Разделяем пакет пополам и рекурсивно обрабатываем
                            mid = len(texts) // 2
                            first_half = self._get_embeddings_direct(texts[:mid])
                            second_half = self._get_embeddings_direct(texts[mid:])
                            return first_half + second_half
                        elif response.status_code == 400 and len(texts) == 1:
                            # Если 400 ошибка даже для одного текста, логируем полный запрос и выбрасываем исключение
                            logger.error(f"Received 400 error even for single text. Request failed.")
                            logger.error(f"Text content: {texts[0]}")
                            response.raise_for_status()
                        
                        # Если это последняя попытка, вызываем исключение
                        if retry_count == max_retries - 1:
                            response.raise_for_status()
                        else:
                            logger.warning(f"Retry {retry_count + 1}/{max_retries} after error")
                            retry_count += 1
                            continue
                    
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
                if retry_count == max_retries - 1:
                    raise
                else:
                    logger.warning(f"Retry {retry_count + 1}/{max_retries} after HTTP error")
                    retry_count += 1
            except Exception as e:
                logger.error(f"Error getting embeddings: {e}")
                if retry_count == max_retries - 1:
                    raise
                else:
                    logger.warning(f"Retry {retry_count + 1}/{max_retries} after error")
                    retry_count += 1

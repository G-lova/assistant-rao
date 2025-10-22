import numpy as np
from openai import OpenAI


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

        self.client = OpenAI(
            base_url=self.api_url,
            api_key=self.api_key
        )
    
    
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
            requests.HTTPError: Если запрос к API завершился неуспешно (например, 4xx или 5xx).
            KeyError: Если в ответе API отсутствует ожидаемое поле 'embeddings'.
        """
        all_embeddings = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]

            response = self.client.embeddings.create(
                model=self.model,
                input=batch
            )

            batch_embs = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embs)

        return np.array(all_embeddings)

import asyncio
import json
import httpx
import pandas as pd
import requests

from configs.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


class DataFetcher:
    """
    Утилита для получения данных из внешнего API с использованием SQL-запросов.

    Класс инкапсулирует логику HTTP-запроса к серверу данных, обработки ответа и преобразования
    результата в структурированный формат (DataFrame). Предназначен для безопасного и удобного
    доступа к данным экспертизы по заданному идентификатору.
    """
    
    def __init__(self, url, headers):
        """
        Инициализирует объект DataFetcher.

        Args:
            url (str): URL API-эндпоинта для отправки POST-запросов с SQL-запросами.
            headers (dict): Заголовки HTTP-запроса (обычно включают аутентификацию).
        """
        self.url = url
        self.headers = headers

    async def fetch_async_expertise_data(self, sql_query: str, bindings: list):
        async with httpx.AsyncClient() as client:
            response = await client.post(self.url, headers=self.headers, json={"sql": sql_query, "bindings": bindings})
        
            if response.status_code != 200:
                raise Exception(f"Ошибка API: {response.status_code}, {response.text}")
                
            return pd.DataFrame(response.json()["data"])


    def fetch_expertise_data(self, sql_query, bindings=None):
        """
        Извлекает данные экспертизы, выполняя параметризованный SQL-запрос.

        Отправляет POST-запрос на указанный URL с SQL-запросом и подставляемым значением expertise_id.
        Проверяет статус ответа, валидирует формат данных и возвращает результат в виде DataFrame.

        Args:
            sql_query (str): SQL-запрос с плейсхолдером (например, ?), который будет заменён на expertise_id.
            expertise_id (Any): ID экспертизы - значение, подставляемое в SQL-запрос как параметр.
            defaultWorkload (int): Рабочая нагрузка на эксперта, выставляемая при подборе экспертов на экспертизу.

        Raises:
            Exception: Если запрос завершился с ошибкой HTTP (например, 4xx, 5xx).
            ValueError: Если поле 'data' в ответе не является списком (некорректный формат).

        Returns:
            pd.DataFrame: Таблица с результатами выполнения запроса. Каждая строка — запись из БД,
                          каждый столбец — поле из SELECT-списка SQL-запроса.
        """
        data = {
            "sql": sql_query,
            "bindings": bindings or []
        }
        
        response = requests.post(self.url, headers=self.headers, data=json.dumps(data))
        
        if response.status_code != 200:
            raise Exception(f"Ошибка API: {response.status_code}, {response.text}")
        
        result = response.json()
        
        if not isinstance(result['data'], list):
            raise ValueError("Ожидался список записей, но получен некорректный формат")
        
        df = pd.DataFrame(result['data'])
        
        
        if df.empty or df['id'].isna().all() or (df['id'].astype(str) == 'None').all():
            raise Exception("Нет данных для анализа")
        
        return df
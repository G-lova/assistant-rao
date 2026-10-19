import asyncio
import json
import time
from configs.http_client_manager import HTTPClientManager
import pandas as pd
import requests

from configs.host_limiter import host_of, parse_retry_after, query_limiter
from configs.retry_utils import PORTAL_QUERY_RETRY_CONFIG, EISRateLimitError, async_retry, retry_delay

from configs.logger import get_logger


logger = get_logger(__name__)


class DataFetcher:
    """
    Утилита для получения данных из внешнего API с использованием SQL-запросов.

    Класс инкапсулирует логику HTTP-запроса к серверу данных, обработки ответа и преобразования
    результата в структурированный формат (DataFrame). Предназначен для безопасного и удобного
    доступа к данным экспертизы по заданному идентификатору.
    """
    
    def __init__(self, url, headers, http_manager: HTTPClientManager):
        """
        Инициализирует объект DataFetcher.

        Args:
            url (str): URL API-эндпоинта для отправки POST-запросов с SQL-запросами.
            headers (dict): Заголовки HTTP-запроса (обычно включают аутентификацию).
        """
        self.url = url
        self.headers = headers
        self.http_manager = http_manager

    @async_retry(PORTAL_QUERY_RETRY_CONFIG)
    async def fetch_async_expertise_data(self, sql_query: str, bindings: list):
        """Выполняет SQL-запрос через API портала (асинхронно).

        Запросы идут через общий ограничитель процесса (:func:`configs.host_limiter.query_limiter`): после ответа
        429 пауза ставится на весь хост портала (по ``Retry-After`` или ``settings.PORTAL_RATE_LIMIT_DELAY``),
        и запрос повторяется (``PORTAL_QUERY_RETRY_CONFIG``). Другие ошибки не повторяются.

        Args:
            sql_query: SQL с плейсхолдерами ``?``.
            bindings: Значения плейсхолдеров.

        Returns:
            pd.DataFrame: Строки результата.

        Raises:
            EISRateLimitError: 429 после исчерпания повторов.
            Exception: Другой ответ, кроме 200.
        """
        limiter = query_limiter()
        async with limiter.slot(self.url) as host:
            session = self.http_manager.get_session()
            async with session.post(
                    self.url,
                    headers=self.headers,
                    json={"sql": sql_query, "bindings": bindings}
                ) as response:

                if response.status == 429:
                    raise _rate_limited(limiter, host, self.url, response.headers.get("Retry-After"))
                if response.status != 200:
                    raise Exception(f"Ошибка API: {response.status}, {await response.text()}")

                return pd.DataFrame((await response.json())["data"])

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
        
        response = self._post_sync(data)

        if response.status_code != 200:
            raise Exception(f"Ошибка API: {response.status_code}, {response.text}")
        
        result = response.json()
        
        if not isinstance(result['data'], list):
            raise ValueError("Ожидался список записей, но получен некорректный формат")
        
        df = pd.DataFrame(result['data'])
        
        return df


    def _post_sync(self, data: dict):
        """POST к API портала (синхронно) с ожиданием после 429.

        Учитывает общую паузу хоста (её ставят и асинхронные запросы процесса) и после 429 повторяет запрос до
        ``settings.PORTAL_QUERY_RATE_LIMIT_RETRIES`` раз с паузой по ``Retry-After`` / ``PORTAL_RATE_LIMIT_DELAY``.

        Args:
            data: Тело запроса (``sql``, ``bindings``).

        Returns:
            requests.Response: Последний ответ (при исчерпании повторов — ответ 429).
        """
        limiter = query_limiter()
        host = host_of(self.url)
        attempt = 0
        while True:
            left = limiter.cooldown_left(host)
            if left > 0:
                time.sleep(left)
            response = requests.post(self.url, headers=self.headers, data=json.dumps(data))
            if response.status_code != 429 or attempt >= PORTAL_QUERY_RETRY_CONFIG.max_retries:
                return response
            attempt += 1
            error = _rate_limited(limiter, host, self.url, response.headers.get("Retry-After"))
            pause = retry_delay(PORTAL_QUERY_RETRY_CONFIG, attempt, error)
            logger.warning(f"{error}; повтор {attempt}/{PORTAL_QUERY_RETRY_CONFIG.max_retries} через {pause:.1f} с")
            time.sleep(pause)


def _rate_limited(limiter, host: str, url: str, retry_after_header) -> EISRateLimitError:
    """Ставит паузу на хост после 429 и возвращает исключение для повтора.

    Args:
        limiter: Ограничитель (:class:`configs.host_limiter.HostLimiter`).
        host: Хост запроса.
        url: Адрес запроса (для сообщения).
        retry_after_header: Значение заголовка ``Retry-After`` (может отсутствовать).

    Returns:
        EISRateLimitError: Исключение с ``retry_after`` (секунды или ``None``).
    """
    from configs import settings
    retry_after = parse_retry_after(retry_after_header)
    limiter.penalize(host, retry_after if retry_after is not None else settings.PORTAL_RATE_LIMIT_DELAY)
    return EISRateLimitError(f"Ошибка API: 429 (слишком много запросов): {url}", retry_after=retry_after)
